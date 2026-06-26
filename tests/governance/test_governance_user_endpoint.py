"""用户级治理快照端点真实 ASGI 测试（FastAPI TestClient）。

覆盖 GET /api/monitoring/governance/{tenant_id}/user/{open_id}：鉴权放行下返回
该用户的生效限额与当日用量。鉴权用 dependency_overrides 放行（鉴权本身已由
tests/tenancy/test_admin_auth.py 等覆盖）。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import smartclaw.governance as governance_pkg
from smartclaw.config.loader import Config, GovernanceConfig
from smartclaw.governance.governor import TenantGovernor
from smartclaw.governance.store import InMemoryStore
from smartclaw import server_monitoring


@pytest.fixture()
def client(monkeypatch):
    cfg = Config(
        governance=GovernanceConfig(
            enabled=True,
            default_user_daily_token_quota=100,
            default_user_max_concurrency=3,
        )
    )
    gov = TenantGovernor(store=InMemoryStore(), config_provider=lambda: cfg)
    # 端点内部 `from smartclaw.governance import get_governor`，打桩该属性
    monkeypatch.setattr(governance_pkg, "get_governor", lambda: gov)

    app = FastAPI()
    app.include_router(server_monitoring.router)
    app.dependency_overrides[server_monitoring.require_monitoring_auth] = lambda: None
    with TestClient(app) as c:
        c._gov = gov  # 暴露给用例预置用量
        yield c


def test_user_governance_snapshot_endpoint(client):
    client._gov.record_user_tokens("acme", "ou_zhang", 30)
    r = client.get("/api/monitoring/governance/acme/user/ou_zhang")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is True
    assert body["tenant_id"] == "acme"
    assert body["open_id"] == "ou_zhang"
    assert body["used_today"] == 30
    assert body["limits"]["daily_token_quota"] == 100
    assert body["limits"]["max_concurrency"] == 3


def test_user_snapshot_distinct_users(client):
    client._gov.record_user_tokens("acme", "ou_a", 10)
    a = client.get("/api/monitoring/governance/acme/user/ou_a").json()
    b = client.get("/api/monitoring/governance/acme/user/ou_b").json()
    assert a["used_today"] == 10
    assert b["used_today"] == 0  # 不同用户互不影响
