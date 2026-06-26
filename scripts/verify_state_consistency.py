# -*- coding: utf-8 -*-
"""多副本「完整状态一致」真实验证（宿主机直跑，见 progress.md §13）。

覆盖一个对话/Agent 在多副本水平扩容下需要一致的三类状态，全部真机跨副本校验：

  ① 控制面：在 app1 开通租户 -> app2 立刻可见、停用即时生效（共享 Redis 租户注册表）。
  ② 数据面 / 结构化记忆：副本 app1 写入一条消息到 PostgreSQL ->
     副本 app2 立刻读到同一条（store=postgres，跨实例同一份）。
  ③ 文件态：
     (a) 副本 app1 在 /root/.smartclaw 写探针文件 -> 副本 app2 立刻读到同一内容（共享卷=用一份）；
     (b) 副本 app1 `smartclaw agent add` 写 agent.json（密文用 ~/.smartclaw/.key 加密）->
         副本 app2 `smartclaw agent list` 看到该 Agent（.key 与 agent.json 均共享，能正常解密）。

前置：先用 docker-compose.ha.yml 起好整套：
    $env:SMARTCLAW_ADMIN_TOKEN = "demo-admin-token-2026"
    docker compose -f docker-compose.ha.yml up -d --build
    python scripts/verify_state_consistency.py
    docker compose -f docker-compose.ha.yml down -v

任一断言失败即非零退出码，便于接入 CI。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

APP1 = os.environ.get("HMC_APP1", "http://127.0.0.1:8001")
APP2 = os.environ.get("HMC_APP2", "http://127.0.0.1:8002")
LB = os.environ.get("HMC_LB", "http://127.0.0.1:8080")
TOKEN = os.environ.get("SMARTCLAW_ADMIN_TOKEN", "demo-admin-token-2026")

C1 = os.environ.get("HMC_CT1", "fc-ha-app1")  # 副本 app1 容器名
C2 = os.environ.get("HMC_CT2", "fc-ha-app2")  # 副本 app2 容器名

TENANT = os.environ.get("HMC_TENANT", "zhixin")
APP_ID = "cli_ha_demo_001"
PROBE_AGENT = "ha_probe_agent"

_PASS = 0
_FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    mark = "PASS" if ok else "FAIL"
    if ok:
        _PASS += 1
    else:
        _FAIL += 1
    print(f"  [{mark}] {label}" + (f" -> {detail}" if detail else ""))


# ----------------------------- HTTP 工具 ----------------------------- #
def _req(method: str, base: str, path: str, body: dict | None = None, auth: bool = True):
    url = base + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if auth:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8") or ""
            return resp.getcode(), _parse(raw)
    except urllib.error.HTTPError as e:
        return e.code, _parse(e.read().decode("utf-8") or "")
    except Exception as exc:
        return 0, str(exc)


def _parse(raw: str):
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return raw


def wait_healthy(base: str, timeout_s: int = 180) -> bool:
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        st, _ = _req("GET", base, "/api/monitoring/health", auth=False)
        if st == 200:
            return True
        last = f"HTTP {st}"
        time.sleep(3)
    print(f"  [FAIL] 等待 {base} 健康超时（最后：{last}）")
    return False


# --------------------------- docker exec 工具 --------------------------- #
def dexec(container: str, args: list[str]) -> tuple[int, str]:
    """在容器内执行命令，返回 (returncode, 合并输出)。"""
    proc = subprocess.run(
        ["docker", "exec", container, *args],
        capture_output=True,
    )
    out = (proc.stdout or b"").decode("utf-8", "replace") + (proc.stderr or b"").decode("utf-8", "replace")
    return proc.returncode, out.strip()


def py_in(container: str, code: str) -> tuple[int, str]:
    """在容器内跑一段 python（smartclaw 已 uv pip install -e .）。"""
    return dexec(container, ["python", "-c", code])


# ------------------------------ 验证主体 ------------------------------ #
def main() -> int:
    print("\n==================== 多副本「完整状态一致」验证 ====================")
    print(f"app1={APP1}  app2={APP2}  lb={LB}")
    print(f"containers: {C1} / {C2}")

    print("\n[0] 等待两副本就绪 ...")
    if not (wait_healthy(APP1) and wait_healthy(APP2)):
        print("\n[ABORT] 副本未就绪。请先 `docker compose -f docker-compose.ha.yml up -d --build`。")
        return 2
    check("app1 / app2 健康检查 200", True)

    # ---------------- ① 控制面 ---------------- #
    print("\n[1] 控制面：app1 开通租户 -> app2 立刻可见、停用即时生效 ...")
    _req("DELETE", APP1, f"/api/admin/tenants/{TENANT}")
    st_c, _ = _req("POST", APP1, "/api/admin/tenants", {
        "tenant_id": TENANT,
        "display_name": "智信电池厂",
        "status": "active",
        "limits": {"daily_token_quota": 500000, "max_concurrency": 8},
        "app_ids": [APP_ID],
    })
    check("app1 开通租户 = 201", st_c == 201, f"HTTP {st_c}")
    st_g, body_g = _req("GET", APP2, f"/api/admin/tenants/{TENANT}")
    check("app2 立刻可见该租户 = 200", st_g == 200, f"HTTP {st_g}")
    if isinstance(body_g, dict):
        check("app2 看到的展示名一致", body_g.get("display_name") == "智信电池厂",
              repr(body_g.get("display_name")))
    st_s, _ = _req("POST", APP2, f"/api/admin/tenants/{TENANT}/suspend")
    check("app2 停用 = 200", st_s == 200, f"HTTP {st_s}")
    st_g1, body_g1 = _req("GET", APP1, f"/api/admin/tenants/{TENANT}")
    seen = body_g1.get("status") if isinstance(body_g1, dict) else None
    check("app1 跨副本看到 status=suspended", seen == "suspended", repr(seen))

    # ---------------- ② 数据面 / 结构化记忆 ---------------- #
    print("\n[2] 数据面：app1 写 PostgreSQL 消息 -> app2 立刻读到同一条 ...")
    marker = f"cross-replica-marker-{int(time.time())}"
    write_code = (
        "import os;"
        "from smartclaw.memory.storage.postgres_store import PostgresStore;"
        "s=PostgresStore(dsn=os.environ['SMARTCLAW_MEMORY_POSTGRES_DSN'], agent_id='ha_probe');"
        "s.initialize();"
        f"s.add_message('ha_sess','user','{marker}', tenant_id='{TENANT}');"
        "print('WROTE')"
    )
    rc_w, out_w = py_in(C1, write_code)
    check("app1 写入 PG 成功", rc_w == 0 and "WROTE" in out_w, out_w[-200:])
    read_code = (
        "import os;"
        "from smartclaw.memory.storage.postgres_store import PostgresStore;"
        "s=PostgresStore(dsn=os.environ['SMARTCLAW_MEMORY_POSTGRES_DSN'], agent_id='ha_probe');"
        "s.initialize();"
        f"ms=s.get_messages('ha_sess', tenant_id='{TENANT}');"
        "print([m['content'] for m in ms][-3:])"
    )
    rc_r, out_r = py_in(C2, read_code)
    check("app2 在 PG 读到 app1 写的同一条", rc_r == 0 and marker in out_r, out_r[-200:])

    # ---------------- ③ 文件态 (a) 共享卷探针 ---------------- #
    print("\n[3] 文件态(a)：app1 写 /root/.smartclaw 探针 -> app2 读到同一内容（共享卷=用一份）...")
    probe = f"shared-volume-ok-{int(time.time())}"
    rc_pw, _ = dexec(C1, ["sh", "-c", f"echo {probe} > /root/.smartclaw/_consistency_probe.txt"])
    check("app1 写探针文件成功", rc_pw == 0)
    rc_pr, out_pr = dexec(C2, ["cat", "/root/.smartclaw/_consistency_probe.txt"])
    check("app2 读到同一探针内容", rc_pr == 0 and probe in out_pr, out_pr[-120:])

    # ---------------- ③ 文件态 (b) agent.json + .key 跨副本 ---------------- #
    print("\n[4] 文件态(b)：app1 创建 Agent（写 agent.json/加密钥）-> app2 list 可见且能解密 ...")
    dexec(C1, ["smartclaw", "agent", "delete", PROBE_AGENT, "--tenant", TENANT, "--force"])  # 起点干净（容错）
    rc_add, out_add = dexec(C1, [
        "smartclaw", "agent", "add", PROBE_AGENT,
        "--tenant", TENANT,
        "--app-id", "cli_haprobe001",
        "--app-secret", "haprobesecretxyz01",
        "--llm-model", "glm-5",
        "--llm-api-key", "dummy-key-not-called",
        "--no-sandbox",
    ])
    check("app1 创建 Agent 成功", rc_add == 0, out_add.splitlines()[-1] if out_add else "")
    # app2 经 AgentManager 读到 app1 写入的 agent.json（共享卷=用一份）。
    # 不解析 rich 表格文本（列宽会截断），直接核对配置实体，稳健且更贴近运行时。
    list_code = (
        "from smartclaw.agent.manager import AgentManager;"
        "m=AgentManager();"
        f"c=m._read_config('{PROBE_AGENT}', tenant_id='{TENANT}') or {{}};"
        "print('NAME=' + str(c.get('name','')) + ' TENANT=' + str(c.get('tenant_id','')))"
    )
    rc_ls, out_ls = py_in(C2, list_code)
    check("app2 读到该 Agent 的 agent.json（共享卷）",
          rc_ls == 0 and f"NAME={PROBE_AGENT}" in out_ls and f"TENANT={TENANT}" in out_ls,
          (out_ls[-160:] if out_ls else ""))
    # 用 app2 的 .key 解密 app1 写入的 app_secret，证明加密钥也是「同一份」
    decrypt_code = (
        "from smartclaw.agent.manager import AgentManager;"
        "m=AgentManager();"
        f"c=m._read_config('{PROBE_AGENT}', tenant_id='{TENANT}') or {{}};"
        "fl=c.get('feishu') or {};"
        "raw=fl.get('app_secret') or c.get('app_secret') or '';"
        "sec=m._decrypt(raw[4:]) if raw.startswith('ENC:') else raw;"
        "print('SECRET=' + str(sec))"
    )
    rc_dec, out_dec = py_in(C2, decrypt_code)
    check("app2 用共享 .key 正确解密 app_secret", rc_dec == 0 and "haprobesecretxyz01" in out_dec,
          out_dec[-160:])

    # ---------------- 清理 ---------------- #
    print("\n[5] 清理 ...")
    _req("DELETE", APP1, f"/api/admin/tenants/{TENANT}")
    dexec(C1, ["smartclaw", "agent", "delete", PROBE_AGENT, "--tenant", TENANT, "--force"])
    dexec(C1, ["rm", "-f", "/root/.smartclaw/_consistency_probe.txt"])

    print(f"\n==================== 结果：{_PASS} passed / {_FAIL} failed ====================")
    if _FAIL == 0:
        print("[state-consistency CLOSED-LOOP OK] 控制面 / 数据面 / 文件态 三者多副本一致。")
        return 0
    print("[state-consistency FAILED] 见上方 FAIL 项。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
