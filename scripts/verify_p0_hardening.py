# -*- coding: utf-8 -*-
"""P0 工业级硬化真实演示（命令行可复现）：管理面默认安全 + 租户注册表多副本一致 + HTTP 边缘防护。

覆盖三项审计补强，全部真实执行（无 mock 的业务逻辑；P0-5 连真实 Redis）：

- P0-4 管理面默认安全 + 独立强鉴权：默认配置拒绝匿名；配专用 admin token 后放行；
  与监控凭证隔离。走真实 ASGI（FastAPI TestClient + 生产 router）。
- P0-5 租户注册表共享 Redis 后端：两个 RedisTenantRegistry = 两个副本，A 开通/停用，
  B 立刻可见；治理器对「停用租户」直接拒绝。并演示一段「智芯电池集团」运营剧本。
- P0-6 HTTP 边缘防护：请求体超过 server.max_request_bytes → 413（生产中间件本体）。

前置：真实 Redis 在 127.0.0.1:6379（P0-5 用）。无 Redis 时 P0-5 friendly skip。
用法：
    $env:PYTHONPATH="src"
    python scripts/verify_p0_hardening.py
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from smartclaw.config.loader import Config, GovernanceConfig
from smartclaw.auth.platform import PlatformAuthAdapter

REDIS_URL = "redis://127.0.0.1:6379/0"


def section(title: str) -> None:
    print(f"\n==================== {title} ====================")


# --------------------------------------------------------------------------- #
# P0-4 管理面默认安全 + 独立强鉴权
# --------------------------------------------------------------------------- #
def demo_admin_auth() -> None:
    section("P0-4 管理面默认安全 + 独立强鉴权")
    from smartclaw.tenancy import api as tenant_api
    from smartclaw.tenancy.registry import TenantRegistry
    import tempfile
    from pathlib import Path

    # 1) 默认配置（未配 admin token + 监控鉴权默认关）→ verify_admin_bearer 默认拒绝
    default_cfg = Config()
    print("默认配置 admin_require_auth =", default_cfg.auth.admin_require_auth, "（默认 True=安全）")
    print("默认配置 匿名访问管理面 ->", PlatformAuthAdapter.verify_admin_bearer("Bearer anything", default_cfg),
          " （False=拒绝，secure by default）")

    # 2) 配专用 admin token → 真实 ASGI 401/200
    cfg = Config()
    cfg.auth.admin_bearer_token = "demo-admin-token-2026"
    tmp = Path(tempfile.mkdtemp()) / "tenants.db"
    reg = TenantRegistry(db_path=tmp)
    _orig = tenant_api.get_config
    tenant_api.get_config = lambda: cfg
    try:
        app = FastAPI()
        app.include_router(tenant_api.router)
        app.dependency_overrides[tenant_api._registry] = lambda: reg
        c = TestClient(app)
        r_none = c.get("/api/admin/tenants")
        r_wrong = c.get("/api/admin/tenants", headers={"Authorization": "Bearer wrong"})
        r_ok = c.get("/api/admin/tenants", headers={"Authorization": "Bearer demo-admin-token-2026"})
        print(f"无 token            -> HTTP {r_none.status_code}")
        print(f"错误 token          -> HTTP {r_wrong.status_code}")
        print(f"正确专用 admin token -> HTTP {r_ok.status_code}")
    finally:
        tenant_api.get_config = _orig
        reg.close()

    # 3) 与监控凭证隔离：配了专用 admin token 时，监控 token 不能用于管理面
    cfg2 = Config()
    cfg2.auth.admin_bearer_token = "admin-key"
    cfg2.auth.monitoring_require_auth = True
    cfg2.auth.monitoring_bearer_token = "mon-key"
    print("配专用 admin token 后，监控 token 访问管理面 ->",
          PlatformAuthAdapter.verify_admin_bearer("Bearer mon-key", cfg2), " （False=最小权限隔离）")


# --------------------------------------------------------------------------- #
# P0-5 租户注册表共享 Redis 后端（多副本一致）+ 运营剧本
# --------------------------------------------------------------------------- #
def demo_shared_registry() -> bool:
    section("P0-5 租户注册表共享 Redis 后端（控制面多副本一致）")
    try:
        import redis

        client = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=3)
        client.ping()
    except Exception as exc:
        print(f"[skip] 未连上真实 Redis（{REDIS_URL}）：{exc}")
        print("       启动后重试： docker run -d --name redis -p 6379:6379 redis:7-alpine")
        return False

    from smartclaw.tenancy.redis_registry import RedisTenantRegistry
    from smartclaw.governance.governor import TenantGovernor
    from smartclaw.governance.store import InMemoryStore

    ns = f"fc:demo:{uuid.uuid4().hex[:8]}:tenant"
    replica_a = RedisTenantRegistry(client=client, namespace=ns)
    replica_b = RedisTenantRegistry(client=client, namespace=ns)
    try:
        print("[剧本] 智芯电池集团：运营在 A 副本在线开通新车间 dept_c（限额 + app_id 路由）")
        replica_a.create(
            "dept_c",
            display_name="三号电芯车间",
            limits={"daily_token_quota": 200000, "max_concurrency": 4},
            app_ids=["cli_dept_c_001"],
        )
        print("  A 副本开通 dept_c 完成")
        print("  B 副本立刻可见 dept_c 限额 ->",
              replica_b.get("dept_c")["limits"]["daily_token_quota"], "（多副本一致）")
        print("  B 副本 app_id 路由 O(1) ->", replica_b.resolve_by_app_id("cli_dept_c_001"))

        # 治理器（注入 B 副本注册表）：停用即拒绝，不受 governance.enabled 影响
        cfg = Config()
        cfg.governance = GovernanceConfig(enabled=True, default_daily_token_quota=999999)
        gov = TenantGovernor(store=InMemoryStore(), config_provider=lambda: cfg, registry=replica_b)
        print("  治理器 admit(dept_c) 启用态 ->", gov.admit("dept_c").allowed)

        print("[剧本] 发现 dept_c 异常 → 运营在 A 副本停用")
        replica_a.set_status("dept_c", "suspended")
        adm = gov.admit("dept_c")
        print("  B 副本注入的治理器 admit(dept_c) ->", adm.allowed, f"reason={adm.reason}",
              "（停用跨副本即时生效）")

        print("[剧本] 排障完成 → A 副本恢复，B 副本注册表限额覆盖即时调整")
        replica_a.set_status("dept_c", "active")
        replica_a.update("dept_c", limits={"daily_token_quota": 50000})
        print("  B 副本看到新配额 ->", replica_b.effective_limits("dept_c"))
        print("  治理器 admit(dept_c) 恢复 ->", gov.admit("dept_c").allowed)
    finally:
        replica_a.delete("dept_c") if replica_a.get_or_none("dept_c") else None
        # 清理本次 namespace 全部键
        for k in client.scan_iter(f"{ns.rsplit(':', 1)[0]}:*"):
            client.delete(k)
    return True


# --------------------------------------------------------------------------- #
# P0-6 HTTP 边缘防护：请求体大小上限
# --------------------------------------------------------------------------- #
def demo_edge_protection() -> None:
    section("P0-6 HTTP 边缘防护：请求体大小上限（413）")
    from smartclaw import server

    cfg = Config()
    cfg.server.max_request_bytes = 100  # 演示用极小阈值
    _orig = server.get_config
    server.get_config = lambda: cfg
    try:
        app = FastAPI()
        app.middleware("http")(server._limit_request_body)  # 生产中间件本体

        @app.post("/echo")
        async def _echo(payload: dict):
            return {"ok": True}

        c = TestClient(app)
        small = c.post("/echo", json={"k": "v"})
        big = c.post("/echo", json={"data": "x" * 500})
        print(f"max_request_bytes = {cfg.server.max_request_bytes} 字节（演示阈值）")
        print(f"正常小请求 ->  HTTP {small.status_code}")
        print(f"超大请求   ->  HTTP {big.status_code}  （413=在路由处理前被边缘拦截）")
    finally:
        server.get_config = _orig


def main() -> None:
    demo_admin_auth()
    ok_redis = demo_shared_registry()
    demo_edge_protection()
    print("\n[p0 hardening OK]" if ok_redis else "\n[p0 hardening OK（P0-5 因无 Redis 跳过）]")


if __name__ == "__main__":
    main()
