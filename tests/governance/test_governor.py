"""TenantGovernor 单元测试（注入配置 + 内存 Store，无全局状态，无网络）。"""

from __future__ import annotations

from smartclaw.config.loader import Config, GovernanceConfig, TenantGovernanceLimit
from smartclaw.governance.governor import TenantGovernor
from smartclaw.governance.store import InMemoryStore


def _governor(gov_cfg: GovernanceConfig) -> TenantGovernor:
    cfg = Config(governance=gov_cfg)
    return TenantGovernor(store=InMemoryStore(), config_provider=lambda: cfg)


def test_disabled_governance_allows_everything():
    g = _governor(GovernanceConfig(enabled=False, default_daily_token_quota=1))
    g.record_tokens("acme", 9999)  # 关闭时不累计
    adm = g.admit("acme")
    assert adm.allowed is True
    assert g.acquire("acme").allowed is True
    assert g.snapshot("acme") == {"enabled": False, "tenant_id": "acme"}


def test_quota_blocks_after_usage_reaches_limit():
    g = _governor(GovernanceConfig(enabled=True, default_daily_token_quota=100))
    assert g.admit("acme").allowed is True
    g.record_tokens("acme", 100)
    adm = g.admit("acme")
    assert adm.allowed is False
    assert adm.reason == "quota_exceeded"
    assert adm.user_message  # 有中文友好提示


def test_rate_limit_blocks_burst():
    # 每分钟 60 次，未配 burst → 容量取 60；一次性打 61 次应在某次被拒。
    g = _governor(GovernanceConfig(enabled=True, default_rate_per_min=60))
    results = [g.admit("acme").allowed for _ in range(61)]
    assert results.count(True) == 60
    assert results[-1] is False


def test_concurrency_limit_blocks_and_releases():
    g = _governor(GovernanceConfig(enabled=True, default_max_concurrency=2))
    assert g.acquire("acme").allowed is True
    assert g.acquire("acme").allowed is True
    denied = g.acquire("acme")
    assert denied.allowed is False
    assert denied.reason == "concurrency_limited"
    g.release("acme")
    assert g.acquire("acme").allowed is True


def test_per_tenant_override_beats_default():
    cfg = GovernanceConfig(
        enabled=True,
        default_daily_token_quota=0,  # 默认不限
        per_tenant={"vip": TenantGovernanceLimit(daily_token_quota=10)},
    )
    g = _governor(cfg)
    # 默认租户不限
    g.record_tokens("free", 1000)
    assert g.admit("free").allowed is True
    # vip 被限额 10
    g.record_tokens("vip", 10)
    assert g.admit("vip").allowed is False


def test_quota_is_per_tenant_isolated():
    g = _governor(GovernanceConfig(enabled=True, default_daily_token_quota=50))
    g.record_tokens("a", 50)
    assert g.admit("a").allowed is False
    assert g.admit("b").allowed is True  # 互不影响


def test_snapshot_reports_limits_and_usage():
    g = _governor(GovernanceConfig(enabled=True, default_daily_token_quota=100, default_max_concurrency=4))
    g.record_tokens("acme", 30)
    snap = g.snapshot("acme")
    assert snap["enabled"] is True
    assert snap["used_today"] == 30
    assert snap["limits"]["daily_token_quota"] == 100
    assert snap["limits"]["max_concurrency"] == 4
