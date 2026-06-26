# -*- coding: utf-8 -*-
"""多副本「控制面共享真相」真实验证（宿主机直跑，零第三方依赖，见 progress.md §12.3）。

前置：先用 docker-compose.replicas.yml 起好 redis + app1(8001) + app2(8002) + nginx LB(8080)：

    $env:SMARTCLAW_ADMIN_TOKEN = "demo-admin-token-2026"
    docker compose -f docker-compose.replicas.yml up -d --build
    python scripts/verify_multi_replica.py

验证链（全部走真实 HTTP）：
  1) 等待两副本 /api/monitoring/health = 200
  2) 在 app1 开通租户              -> app2 立刻 GET 可见（多副本一致：同一 Redis 租户注册表）
  3) 在 app2 停用该租户            -> app1 GET 状态 = suspended（跨副本即时生效）
  4) 经 nginx LB 轮询多次访问      -> 命中不同上游副本，结果均一致
  5) 清理（DELETE）

任一断言失败即非零退出码，便于接入 CI。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

APP1 = os.environ.get("HMC_APP1", "http://127.0.0.1:8001")
APP2 = os.environ.get("HMC_APP2", "http://127.0.0.1:8002")
LB = os.environ.get("HMC_LB", "http://127.0.0.1:8080")
TOKEN = os.environ.get("SMARTCLAW_ADMIN_TOKEN", "demo-admin-token-2026")
TENANT = os.environ.get("HMC_TENANT", "mr_demo_tenant")
APP_ID = "cli_mr_demo_001"

_PASS = 0
_FAIL = 0


def _req(method: str, base: str, path: str, body: dict | None = None, auth: bool = True):
    """返回 (status_code, json_or_text, headers)。不抛 HTTPError（把 4xx/5xx 当正常返回）。"""
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
            status = resp.getcode()
            headers = dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8") or ""
        status = e.code
        headers = dict(e.headers or {})
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = raw
    return status, payload, headers


def check(label: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    mark = "PASS" if ok else "FAIL"
    if ok:
        _PASS += 1
    else:
        _FAIL += 1
    print(f"  [{mark}] {label}" + (f" -> {detail}" if detail else ""))


def wait_healthy(base: str, timeout_s: int = 150) -> bool:
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        try:
            status, _, _ = _req("GET", base, "/api/monitoring/health", auth=False)
            if status == 200:
                return True
            last = f"HTTP {status}"
        except Exception as exc:  # 容器未起完，连接被拒
            last = str(exc)
        time.sleep(3)
    print(f"  [FAIL] 等待 {base} 健康超时（最后：{last}）")
    return False


def main() -> int:
    print(f"\n==================== 多副本控制面一致性验证 ====================")
    print(f"app1={APP1}  app2={APP2}  lb={LB}  tenant={TENANT}")

    print("\n[1] 等待两副本就绪 ...")
    if not (wait_healthy(APP1) and wait_healthy(APP2)):
        print("\n[ABORT] 副本未就绪。请先 `docker compose -f docker-compose.replicas.yml up -d --build`。")
        return 2
    check("app1 / app2 健康检查 200", True)

    # 起点干净：若残留先删
    _req("DELETE", APP1, f"/api/admin/tenants/{TENANT}")

    print("\n[2] 在 app1 开通租户，验证 app2 立刻可见 ...")
    st_c, body_c, _ = _req("POST", APP1, "/api/admin/tenants", {
        "tenant_id": TENANT,
        "display_name": "多副本演示租户",
        "status": "active",
        "limits": {"daily_token_quota": 200000, "max_concurrency": 4},
        "app_ids": [APP_ID],
    })
    check("app1 POST 开通租户 = 201", st_c == 201, f"HTTP {st_c}")

    st_g2, body_g2, _ = _req("GET", APP2, f"/api/admin/tenants/{TENANT}")
    check("app2 GET 立刻可见该租户 = 200", st_g2 == 200, f"HTTP {st_g2}")
    if isinstance(body_g2, dict):
        check("app2 看到的展示名一致", body_g2.get("display_name") == "多副本演示租户",
              repr(body_g2.get("display_name")))
        limits = (body_g2.get("limits") or {})
        check("app2 看到的配额一致 (=200000)", limits.get("daily_token_quota") == 200000,
              str(limits.get("daily_token_quota")))

    # app_id 路由跨副本一致
    st_a2, body_a2, _ = _req("GET", APP2, f"/api/admin/tenants/{TENANT}")
    app_ids_seen = (body_a2.get("app_ids") if isinstance(body_a2, dict) else None) or []
    check("app2 看到 app_id 路由一致", APP_ID in app_ids_seen, str(app_ids_seen))

    print("\n[3] 在 app2 停用，验证 app1 跨副本即时生效 ...")
    st_s, _, _ = _req("POST", APP2, f"/api/admin/tenants/{TENANT}/suspend")
    check("app2 POST 停用 = 200", st_s == 200, f"HTTP {st_s}")
    st_g1, body_g1, _ = _req("GET", APP1, f"/api/admin/tenants/{TENANT}")
    status_seen = body_g1.get("status") if isinstance(body_g1, dict) else None
    check("app1 GET 状态 = suspended（跨副本即时）", status_seen == "suspended", repr(status_seen))

    print("\n[4] 经 nginx LB 多次访问，结果始终一致 ...")
    upstreams = set()
    statuses = set()
    for _ in range(8):
        st_lb, body_lb, hdrs = _req("GET", LB, f"/api/admin/tenants/{TENANT}")
        if st_lb == 200 and isinstance(body_lb, dict):
            statuses.add(body_lb.get("status"))
        up = hdrs.get("X-Upstream") or hdrs.get("x-upstream")
        if up:
            upstreams.add(up)
        time.sleep(0.2)
    # 硬断言：LB 命中的结果必须始终一致（这才是「共享真相」的保证，与命中哪个副本无关）。
    check("LB 所有命中结果一致 (=suspended)", statuses == {"suspended"}, f"statuses={sorted(statuses)}")
    # 观测项（非阻断）：上游分摊依赖客户端连接行为，仅作信息展示。
    print(f"  [NOTE] 本次 LB 观测到的上游副本：{sorted(upstreams)}"
          f"（轮询是否分摊取决于客户端连接复用，不影响一致性保证）")

    print("\n[5] 清理 ...")
    st_d, _, _ = _req("DELETE", APP1, f"/api/admin/tenants/{TENANT}")
    check("app1 DELETE 清理 = 204", st_d == 204, f"HTTP {st_d}")
    st_gone, _, _ = _req("GET", APP2, f"/api/admin/tenants/{TENANT}")
    check("app2 确认已删除 = 404（删除也跨副本一致）", st_gone == 404, f"HTTP {st_gone}")

    print(f"\n==================== 结果：{_PASS} passed / {_FAIL} failed ====================")
    if _FAIL == 0:
        print("[multi-replica CLOSED-LOOP OK] 控制面在多副本间共享真相、即时一致。")
        return 0
    print("[multi-replica FAILED] 见上方 FAIL 项。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
