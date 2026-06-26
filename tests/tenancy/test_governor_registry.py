"""治理器 × 租户注册表 集成测试（停用拒绝 + 限额覆盖）。"""

from __future__ import annotations

from smartclaw.config.loader import Config, GovernanceConfig
from smartclaw.governance.governor import TenantGovernor
from smartclaw.governance.store import InMemoryStore
from smartclaw.tenancy.registry import TenantRegistry


def _gov(governance: GovernanceConfig, registry: TenantRegistry) -> TenantGovernor:
    cfg = Config()
    cfg.governance = governance
    return TenantGovernor(
        store=InMemoryStore(), config_provider=lambda: cfg, registry=registry
    )


def test_suspended_tenant_denied_even_when_governance_disabled(tmp_path):
    reg = TenantRegistry(db_path=tmp_path / "t.db")
    reg.create("acme")
    reg.set_status("acme", "suspended")
    # governance.enabled=False：限流/配额旁路，但停用仍必须拦截。
    gov = _gov(GovernanceConfig(enabled=False), reg)
    adm = gov.admit("acme")
    assert adm.allowed is False
    assert adm.reason == "tenant_suspended"
    # 未停用的租户照常放行
    assert gov.admit("other").allowed is True
    reg.close()


def test_registry_quota_override_takes_effect(tmp_path):
    reg = TenantRegistry(db_path=tmp_path / "t.db")
    # 全局默认配额 10000，但注册表把 acme 压到 5
    reg.create("acme", limits={"daily_token_quota": 5})
    gov = _gov(
        GovernanceConfig(enabled=True, default_daily_token_quota=10000), reg
    )
    snap = gov.snapshot("acme")
    assert snap["limits"]["daily_token_quota"] == 5  # 注册表覆盖生效

    # 消耗到上限后被拒
    gov.record_tokens("acme", 5)
    adm = gov.admit("acme")
    assert adm.allowed is False
    assert adm.reason == "quota_exceeded"
    reg.close()


def test_no_registry_keeps_pure_config_behavior(tmp_path):
    # registry=None：完全退回配置行为，不创建任何 DB
    cfg = Config()
    cfg.governance = GovernanceConfig(enabled=True, default_daily_token_quota=100)
    gov = TenantGovernor(
        store=InMemoryStore(), config_provider=lambda: cfg, registry=None
    )
    assert gov.admit("acme").allowed is True
    assert gov.snapshot("acme")["limits"]["daily_token_quota"] == 100
