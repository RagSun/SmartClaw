"""TenantRegistry 单元测试（临时 SQLite，无网络）。"""

from __future__ import annotations

import pytest

from smartclaw.tenancy.registry import (
    TenantExistsError,
    TenantNotFoundError,
    TenantRegistry,
)


@pytest.fixture()
def reg(tmp_path):
    r = TenantRegistry(db_path=tmp_path / "tenants.db")
    yield r
    r.close()


def test_create_get_list(reg):
    rec = reg.create(
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

    got = reg.get("acme")
    assert got["display_name"] == "Acme 工厂"
    assert [t["tenant_id"] for t in reg.list()] == ["acme"]


def test_create_duplicate_raises(reg):
    reg.create("acme")
    with pytest.raises(TenantExistsError):
        reg.create("acme")


def test_tenant_id_normalized(reg):
    rec = reg.create("Acme Battery!!")  # 含空格/特殊字符 → 规范化
    assert rec["tenant_id"] == "Acme_Battery"
    assert reg.get("Acme Battery!!")["tenant_id"] == "Acme_Battery"


def test_update_partial_and_status(reg):
    reg.create("acme", limits={"daily_token_quota": 100})
    upd = reg.update("acme", display_name="新名", limits={"rate_per_min": 30})
    assert upd["display_name"] == "新名"
    assert upd["limits"]["rate_per_min"] == 30
    assert upd["limits"]["daily_token_quota"] == 100  # 未传的字段保持不变

    reg.set_status("acme", "suspended")
    assert reg.is_suspended("acme") is True
    reg.set_status("acme", "active")
    assert reg.is_suspended("acme") is False


def test_update_invalid_status_raises(reg):
    reg.create("acme")
    with pytest.raises(ValueError):
        reg.update("acme", status="frozen")


def test_delete(reg):
    reg.create("acme")
    reg.delete("acme")
    with pytest.raises(TenantNotFoundError):
        reg.get("acme")
    with pytest.raises(TenantNotFoundError):
        reg.delete("acme")  # 再删不存在 → 报错


def test_resolve_by_app_id(reg):
    reg.create("acme", app_ids=["cli_a", "cli_b"])
    reg.create("globex", app_ids=["cli_c"])
    assert reg.resolve_by_app_id("cli_b") == "acme"
    assert reg.resolve_by_app_id("cli_c") == "globex"
    assert reg.resolve_by_app_id("unknown") is None
    assert reg.resolve_by_app_id("") is None


def test_effective_limits_only_non_null(reg):
    reg.create("acme", limits={"daily_token_quota": 500})
    eff = reg.effective_limits("acme")
    assert eff == {"daily_token_quota": 500}  # 仅非空项
    assert reg.effective_limits("nope") is None  # 无记录


def test_persistence_across_instances(tmp_path):
    db = tmp_path / "tenants.db"
    r1 = TenantRegistry(db_path=db)
    r1.create("acme", limits={"daily_token_quota": 9})
    r1.close()
    r2 = TenantRegistry(db_path=db)  # 重新打开同一文件
    assert r2.get("acme")["limits"]["daily_token_quota"] == 9
    r2.close()
