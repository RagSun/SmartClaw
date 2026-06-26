"""租户管理 API 真实 ASGI 测试（FastAPI TestClient + 临时 SQLite）。

TestClient 走真实 Starlette/FastAPI 路由、序列化与依赖注入，覆盖完整 CRUD 生命周期
与鉴权。注册表用临时库，通过 dependency_overrides 注入，避免触碰真实数据目录。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from smartclaw.tenancy import api as tenant_api
from smartclaw.tenancy.registry import TenantRegistry


@pytest.fixture()
def client(tmp_path):
    reg = TenantRegistry(db_path=tmp_path / "tenants.db")
    app = FastAPI()
    app.include_router(tenant_api.router)
    app.dependency_overrides[tenant_api._registry] = lambda: reg
    # CRUD 用例不测鉴权 → 放行；鉴权单独用 test_auth_required 验证。
    app.dependency_overrides[tenant_api.require_admin_auth] = lambda: None
    with TestClient(app) as c:
        yield c
    reg.close()


def test_full_lifecycle(client):
    # 开通
    r = client.post(
        "/api/admin/tenants",
        json={
            "tenant_id": "acme",
            "display_name": "Acme 工厂",
            "limits": {"daily_token_quota": 1000, "max_concurrency": 2},
            "app_ids": ["cli_acme"],
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["tenant_id"] == "acme"

    # 重复开通 → 409
    assert client.post("/api/admin/tenants", json={"tenant_id": "acme"}).status_code == 409

    # 列表
    r = client.get("/api/admin/tenants")
    assert r.status_code == 200
    assert [t["tenant_id"] for t in r.json()["tenants"]] == ["acme"]

    # 详情
    assert client.get("/api/admin/tenants/acme").json()["display_name"] == "Acme 工厂"
    assert client.get("/api/admin/tenants/nope").status_code == 404

    # 改配额
    r = client.patch(
        "/api/admin/tenants/acme", json={"limits": {"daily_token_quota": 50}}
    )
    assert r.status_code == 200
    assert r.json()["limits"]["daily_token_quota"] == 50

    # 停用 / 启用
    assert client.post("/api/admin/tenants/acme/suspend").json()["status"] == "suspended"
    assert client.post("/api/admin/tenants/acme/activate").json()["status"] == "active"

    # 删除
    assert client.delete("/api/admin/tenants/acme").status_code == 204
    assert client.get("/api/admin/tenants/acme").status_code == 404


def test_create_validation_error(client):
    # 缺 tenant_id → 422
    assert client.post("/api/admin/tenants", json={}).status_code == 422
    # 负配额 → 422（ge=0）
    r = client.post(
        "/api/admin/tenants",
        json={"tenant_id": "x", "limits": {"daily_token_quota": -1}},
    )
    assert r.status_code == 422


def test_auth_required(tmp_path, monkeypatch):
    """开启鉴权后，无/错 Bearer → 401，正确 Bearer → 放行。"""
    reg = TenantRegistry(db_path=tmp_path / "tenants.db")

    class _Auth:
        monitoring_require_auth = True
        monitoring_jwt_enabled = False
        monitoring_bearer_token = "s3cret"

    class _Cfg:
        auth = _Auth()

    monkeypatch.setattr(tenant_api, "get_config", lambda: _Cfg())

    app = FastAPI()
    app.include_router(tenant_api.router)
    app.dependency_overrides[tenant_api._registry] = lambda: reg
    with TestClient(app) as c:
        assert c.get("/api/admin/tenants").status_code == 401
        assert c.get(
            "/api/admin/tenants", headers={"Authorization": "Bearer wrong"}
        ).status_code == 401
        assert c.get(
            "/api/admin/tenants", headers={"Authorization": "Bearer s3cret"}
        ).status_code == 200
    reg.close()
