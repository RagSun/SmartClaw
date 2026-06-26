"""用户级配额单元测试（纯增量）：租户管钱 + 用户管公平/归属。

全部注入配置 + 内存 Store，无全局状态、无网络。重点验证：
- 默认（未配用户级）行为与历史一致（admit_user 恒放行）。
- 用户配额/限流/并发独立生效，且与租户级互不干扰。
- 复合主体键隔离：纯租户键与 ``tenant|u:open_id`` 键计数互不串。
- 租户级优先：租户超限先报租户原因，不被用户层掩盖。
"""

from __future__ import annotations

from smartclaw.config.loader import Config, GovernanceConfig, TenantGovernanceLimit
from smartclaw.governance.governor import (
    TenantGovernor,
    tenant_subject,
    user_subject,
)
from smartclaw.governance.store import InMemoryStore


def _governor(gov_cfg: GovernanceConfig) -> TenantGovernor:
    cfg = Config(governance=gov_cfg)
    return TenantGovernor(store=InMemoryStore(), config_provider=lambda: cfg)


# ----------------------------- 默认/兼容 ----------------------------- #
def test_disabled_governance_user_allows_everything():
    g = _governor(GovernanceConfig(enabled=False, default_user_daily_token_quota=1))
    g.record_user_tokens("acme", "ou_a", 9999)  # 关闭时不累计
    assert g.admit_user("acme", "ou_a").allowed is True
    assert g.acquire_user("acme", "ou_a").allowed is True
    assert g.user_snapshot("acme", "ou_a") == {
        "enabled": False,
        "tenant_id": "acme",
        "open_id": "ou_a",
    }


def test_enabled_but_no_user_limits_allows_everything():
    """治理开启但未配用户级 → admit_user 恒放行（行为与历史一致）。"""
    g = _governor(GovernanceConfig(enabled=True, default_daily_token_quota=100))
    g.record_user_tokens("acme", "ou_a", 999999)
    assert g.admit_user("acme", "ou_a").allowed is True


def test_empty_open_id_is_bypassed():
    """无 open_id（系统/定时任务）→ 用户级整体跳过。"""
    g = _governor(GovernanceConfig(enabled=True, default_user_daily_token_quota=1))
    g.record_user_tokens("acme", "", 100)  # 不累计
    assert g.admit_user("acme", "").allowed is True
    assert g.acquire_user("acme", "").allowed is True


# ----------------------------- 配额 ----------------------------- #
def test_user_quota_blocks_after_usage():
    g = _governor(GovernanceConfig(enabled=True, default_user_daily_token_quota=100))
    assert g.admit_user("acme", "ou_a").allowed is True
    g.record_user_tokens("acme", "ou_a", 100)
    adm = g.admit_user("acme", "ou_a")
    assert adm.allowed is False
    assert adm.reason == "user_quota_exceeded"
    assert adm.user_message


def test_user_quota_is_per_user_isolated():
    g = _governor(GovernanceConfig(enabled=True, default_user_daily_token_quota=50))
    g.record_user_tokens("acme", "ou_a", 50)
    assert g.admit_user("acme", "ou_a").allowed is False
    assert g.admit_user("acme", "ou_b").allowed is True  # 同租户另一用户不受影响


def test_per_user_override_beats_default():
    cfg = GovernanceConfig(
        enabled=True,
        default_user_daily_token_quota=0,  # 默认不限
        per_user_by_tenant={
            "acme": {"ou_intern": TenantGovernanceLimit(daily_token_quota=20)}
        },
    )
    g = _governor(cfg)
    # 默认用户不限
    g.record_user_tokens("acme", "ou_boss", 100000)
    assert g.admit_user("acme", "ou_boss").allowed is True
    # 实习生被限额 20
    g.record_user_tokens("acme", "ou_intern", 20)
    assert g.admit_user("acme", "ou_intern").allowed is False


def test_per_user_override_zero_means_unlimited():
    cfg = GovernanceConfig(
        enabled=True,
        default_user_daily_token_quota=10,  # 默认限 10
        per_user_by_tenant={
            "acme": {"ou_boss": TenantGovernanceLimit(daily_token_quota=0)}
        },
    )
    g = _governor(cfg)
    g.record_user_tokens("acme", "ou_boss", 99999)
    assert g.admit_user("acme", "ou_boss").allowed is True  # 0=不限


# ----------------------------- 限流 ----------------------------- #
def test_user_rate_limit_blocks_burst():
    g = _governor(GovernanceConfig(enabled=True, default_user_rate_per_min=60))
    results = [g.admit_user("acme", "ou_a").allowed for _ in range(61)]
    assert results.count(True) == 60
    assert results[-1] is False
    # 另一个用户独立桶
    assert g.admit_user("acme", "ou_b").allowed is True


# ----------------------------- 并发 ----------------------------- #
def test_user_concurrency_limit_and_release():
    g = _governor(GovernanceConfig(enabled=True, default_user_max_concurrency=2))
    assert g.acquire_user("acme", "ou_a").allowed is True
    assert g.acquire_user("acme", "ou_a").allowed is True
    denied = g.acquire_user("acme", "ou_a")
    assert denied.allowed is False
    assert denied.reason == "user_concurrency_limited"
    g.release_user("acme", "ou_a")
    assert g.acquire_user("acme", "ou_a").allowed is True


# ----------------------------- 租户优先 ----------------------------- #
def test_tenant_quota_reported_before_user():
    """租户到顶时 admit() 先报租户原因，与 admit_user() 各司其职、不互相掩盖。"""
    cfg = GovernanceConfig(
        enabled=True,
        default_daily_token_quota=100,
        default_user_daily_token_quota=10,
    )
    g = _governor(cfg)
    # 模拟真实链路：记一次用量同时累计租户与用户
    g.record_tokens("acme", 100)
    g.record_user_tokens("acme", "ou_a", 10)
    # 租户级先判
    t = g.admit("acme")
    assert t.allowed is False and t.reason == "quota_exceeded"
    # 用户级也判（独立维度）
    u = g.admit_user("acme", "ou_a")
    assert u.allowed is False and u.reason == "user_quota_exceeded"


# ----------------------------- 复合键隔离（零债务关键保证） ----------------------------- #
def test_composite_key_does_not_collide_with_tenant_key():
    """直接断言：纯租户键与用户复合键在 Store 中是两个独立计数。"""
    store = InMemoryStore()
    store.incr_daily_tokens(tenant_subject("acme"), "2026-06-12", 100)
    store.incr_daily_tokens(user_subject("acme", "ou_a"), "2026-06-12", 7)
    assert store.get_daily_tokens(tenant_subject("acme"), "2026-06-12") == 100
    assert store.get_daily_tokens(user_subject("acme", "ou_a"), "2026-06-12") == 7
    # 另一个用户独立
    assert store.get_daily_tokens(user_subject("acme", "ou_b"), "2026-06-12") == 0


def test_tenant_and_user_counters_parallel_in_governor():
    g = _governor(
        GovernanceConfig(
            enabled=True,
            default_daily_token_quota=1000,
            default_user_daily_token_quota=100,
        )
    )
    g.record_tokens("acme", 300)
    g.record_user_tokens("acme", "ou_a", 50)
    assert g.snapshot("acme")["used_today"] == 300
    assert g.user_snapshot("acme", "ou_a")["used_today"] == 50


def test_user_snapshot_reports_limits_and_usage():
    g = _governor(
        GovernanceConfig(
            enabled=True,
            default_user_daily_token_quota=100,
            default_user_max_concurrency=3,
        )
    )
    g.record_user_tokens("acme", "ou_a", 30)
    snap = g.user_snapshot("acme", "ou_a")
    assert snap["enabled"] is True
    assert snap["open_id"] == "ou_a"
    assert snap["used_today"] == 30
    assert snap["limits"]["daily_token_quota"] == 100
    assert snap["limits"]["max_concurrency"] == 3
