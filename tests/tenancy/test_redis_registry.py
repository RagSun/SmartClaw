"""RedisTenantRegistry 真实 Redis 测试（控制面多副本一致，P0-B）。

无 fakeredis、无回退：未设置 ``SMARTCLAW_TEST_REDIS_URL`` 或 Redis 不可达则
friendly skip。每个用例独占 namespace，结束只清理自己的键。

关键证据：``test_two_registries_share_state_simulating_replicas`` 证明在 A 副本
开通 / 停用 / 改配额，B 副本立刻可见——这是本地 SQLite 后端做不到的。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _redis_real import cleanup, connect_or_skip  # noqa: E402

from smartclaw.tenancy.redis_registry import RedisTenantRegistry  # noqa: E402
from smartclaw.tenancy.registry import (  # noqa: E402
    TenantExistsError,
    TenantNotFoundError,
)


@pytest.fixture()
def reg():
    client, ns = connect_or_skip()
    r = RedisTenantRegistry(client=client, namespace=f"{ns}:tenant")
    try:
        yield r, client, ns
    finally:
        cleanup(client, ns)


def test_create_get_list(reg):
    r, _client, _ns = reg
    rec = r.create(
        "acme",
        display_name="Acme 工厂",
        limits={"daily_token_quota": 1000, "max_concurrency": 2},
        app_ids=["cli_app_acme"],
        metadata={"owner": "ops"},
    )
    assert rec["tenant_id"] == "acme"
    assert rec["status"] == "active"
    assert rec["limits"]["daily_token_quota"] == 1000
    assert rec["limits"]["rate_per_min"] is None  # 未设 → 继承默认
    assert rec["app_ids"] == ["cli_app_acme"]
    assert rec["metadata"]["owner"] == "ops"
    assert r.get("acme")["display_name"] == "Acme 工厂"
    assert [t["tenant_id"] for t in r.list()] == ["acme"]


def test_create_duplicate_raises(reg):
    r, _client, _ns = reg
    r.create("acme")
    with pytest.raises(TenantExistsError):
        r.create("acme")


def test_tenant_id_normalized(reg):
    r, _client, _ns = reg
    rec = r.create("Acme Battery!!")
    assert rec["tenant_id"] == "Acme_Battery"
    assert r.get("Acme Battery!!")["tenant_id"] == "Acme_Battery"


def test_update_partial_and_status(reg):
    r, _client, _ns = reg
    r.create("acme", limits={"daily_token_quota": 100})
    upd = r.update("acme", display_name="新名", limits={"rate_per_min": 30})
    assert upd["display_name"] == "新名"
    assert upd["limits"]["rate_per_min"] == 30
    assert upd["limits"]["daily_token_quota"] == 100  # 未传字段保持
    r.set_status("acme", "suspended")
    assert r.is_suspended("acme") is True
    r.set_status("acme", "active")
    assert r.is_suspended("acme") is False


def test_update_invalid_status_raises(reg):
    r, _client, _ns = reg
    r.create("acme")
    with pytest.raises(ValueError):
        r.update("acme", status="frozen")


def test_delete(reg):
    r, _client, _ns = reg
    r.create("acme")
    r.delete("acme")
    with pytest.raises(TenantNotFoundError):
        r.get("acme")
    with pytest.raises(TenantNotFoundError):
        r.delete("acme")


def test_resolve_by_app_id_o1_index(reg):
    r, _client, ns = reg
    r.create("acme", app_ids=["cli_a", "cli_b"])
    r.create("globex", app_ids=["cli_c"])
    assert r.resolve_by_app_id("cli_b") == "acme"
    assert r.resolve_by_app_id("cli_c") == "globex"
    assert r.resolve_by_app_id("unknown") is None
    assert r.resolve_by_app_id("") is None
    # 路由确实走 O(1) hash 索引，而非全表扫描
    assert _client.hget(f"{ns}:tenant_appid", "cli_b") == "acme"


def test_app_id_index_maintained_on_update_and_delete(reg):
    r, _client, _ns = reg
    r.create("acme", app_ids=["cli_old"])
    assert r.resolve_by_app_id("cli_old") == "acme"
    # 改路由：旧 app_id 失效，新 app_id 生效
    r.update("acme", app_ids=["cli_new"])
    assert r.resolve_by_app_id("cli_old") is None
    assert r.resolve_by_app_id("cli_new") == "acme"
    # 删除：索引一并清理
    r.delete("acme")
    assert r.resolve_by_app_id("cli_new") is None


def test_effective_limits_only_non_null(reg):
    r, _client, _ns = reg
    r.create("acme", limits={"daily_token_quota": 500})
    assert r.effective_limits("acme") == {"daily_token_quota": 500}
    assert r.effective_limits("nope") is None


def test_two_registries_share_state_simulating_replicas(reg):
    """两个 RedisTenantRegistry = 两个副本：A 写，B 立刻可见。"""
    r_a, client, ns = reg
    r_b = RedisTenantRegistry(client=client, namespace=f"{ns}:tenant")

    # A 副本开通
    r_a.create("acme", limits={"daily_token_quota": 9}, app_ids=["cli_x"])
    # B 副本立刻看到（身份 / 限额 / 路由）
    assert r_b.get("acme")["limits"]["daily_token_quota"] == 9
    assert r_b.resolve_by_app_id("cli_x") == "acme"

    # A 副本停用 → B 副本立刻认为已停用（停用跨副本生效，安全关键）
    r_a.set_status("acme", "suspended")
    assert r_b.is_suspended("acme") is True

    # B 副本改配额 → A 副本立刻可见
    r_b.update("acme", limits={"daily_token_quota": 3})
    assert r_a.get("acme")["limits"]["daily_token_quota"] == 3


def test_factory_selects_redis_backend(reg):
    """create_tenant_registry：store=redis + redis_url → RedisTenantRegistry。"""
    import os

    from smartclaw.config.loader import GovernanceConfig
    from smartclaw.tenancy.registry import create_tenant_registry

    url = os.environ["SMARTCLAW_TEST_REDIS_URL"]  # reg 已确保可达
    gov = GovernanceConfig(store="redis", redis_url=url)
    backend = create_tenant_registry(gov)
    try:
        assert isinstance(backend, RedisTenantRegistry)
        assert backend.ping() is True
    finally:
        backend.close()


def test_factory_redis_without_url_raises():
    """store=redis 但缺 redis_url → ValueError（绝不静默降级）。"""
    from smartclaw.config.loader import GovernanceConfig
    from smartclaw.tenancy.registry import create_tenant_registry

    with pytest.raises(ValueError):
        create_tenant_registry(GovernanceConfig(store="redis", redis_url=""))
