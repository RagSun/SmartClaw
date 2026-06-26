"""管理面鉴权（P0-A）：默认安全 + 独立强凭证 + 监控回退。

覆盖 verify_admin_bearer 的四条判定路径，以及真实 ASGI 下 /api/admin/tenants 的
401/200 行为。全部纯单元 / TestClient，无网络。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from smartclaw.auth.platform import PlatformAuthAdapter
from smartclaw.config.loader import Config
from smartclaw.tenancy import api as tenant_api
from smartclaw.tenancy.registry import TenantRegistry


def _cfg(**auth_kwargs) -> Config:
    cfg = Config()
    for k, v in auth_kwargs.items():
        setattr(cfg.auth, k, v)
    return cfg


def test_default_config_denies_admin_anonymous():
    """开箱即用（无专用凭证 + 监控鉴权默认关闭）→ 管理面默认拒绝。"""
    cfg = _cfg()  # admin_require_auth 默认 True；无 token；monitoring_require_auth 默认 False
    assert cfg.auth.admin_require_auth is True
    assert PlatformAuthAdapter.verify_admin_bearer(None, cfg) is False
    assert PlatformAuthAdapter.verify_admin_bearer("Bearer anything", cfg) is False


def test_dedicated_admin_token_required_exact():
    cfg = _cfg(admin_bearer_token="adm1n-key")
    assert PlatformAuthAdapter.verify_admin_bearer(None, cfg) is False
    assert PlatformAuthAdapter.verify_admin_bearer("Bearer wrong", cfg) is False
    assert PlatformAuthAdapter.verify_admin_bearer("adm1n-key", cfg) is False  # 缺 Bearer 前缀
    assert PlatformAuthAdapter.verify_admin_bearer("Bearer adm1n-key", cfg) is True


def test_admin_independent_from_monitoring_token():
    """配置了专用 admin token 时，监控凭证不能用于管理面（最小权限隔离）。"""
    cfg = _cfg(
        admin_bearer_token="adm1n-key",
        monitoring_require_auth=True,
        monitoring_bearer_token="mon-key",
    )
    assert PlatformAuthAdapter.verify_admin_bearer("Bearer mon-key", cfg) is False
    assert PlatformAuthAdapter.verify_admin_bearer("Bearer adm1n-key", cfg) is True


def test_fallback_to_monitoring_when_enforced_and_no_admin_token():
    """向后兼容：未配置专用凭证但监控鉴权已强制 → 回退复用监控凭证。"""
    cfg = _cfg(monitoring_require_auth=True, monitoring_bearer_token="mon-key")
    assert PlatformAuthAdapter.verify_admin_bearer("Bearer mon-key", cfg) is True
    assert PlatformAuthAdapter.verify_admin_bearer("Bearer wrong", cfg) is False


def test_explicit_opt_out_allows_all():
    cfg = _cfg(admin_require_auth=False)
    assert PlatformAuthAdapter.verify_admin_bearer(None, cfg) is True


def test_asgi_admin_secure_by_default(tmp_path, monkeypatch):
    """真实 ASGI：默认配置下匿名访问管理面 → 401；配 token 后正确凭证 → 200。"""
    reg = TenantRegistry(db_path=tmp_path / "tenants.db")
    cfg = _cfg(admin_bearer_token="adm1n-key")
    monkeypatch.setattr(tenant_api, "get_config", lambda: cfg)

    app = FastAPI()
    app.include_router(tenant_api.router)
    app.dependency_overrides[tenant_api._registry] = lambda: reg
    with TestClient(app) as c:
        assert c.get("/api/admin/tenants").status_code == 401
        assert c.get(
            "/api/admin/tenants", headers={"Authorization": "Bearer wrong"}
        ).status_code == 401
        assert c.get(
            "/api/admin/tenants", headers={"Authorization": "Bearer adm1n-key"}
        ).status_code == 200
    reg.close()


def test_admin_auth_env_override(monkeypatch):
    """SMARTCLAW_ADMIN_TOKEN / SMARTCLAW_ADMIN_REQUIRE_AUTH 环境变量覆盖。"""
    from smartclaw.config.loader import ConfigLoader

    monkeypatch.setenv("SMARTCLAW_ADMIN_TOKEN", "from-env")
    monkeypatch.setenv("SMARTCLAW_ADMIN_REQUIRE_AUTH", "true")
    cfg = Config()
    ConfigLoader._apply_admin_auth_env(cfg)
    assert cfg.auth.admin_bearer_token == "from-env"
    assert cfg.auth.admin_require_auth is True
    assert PlatformAuthAdapter.verify_admin_bearer("Bearer from-env", cfg) is True
