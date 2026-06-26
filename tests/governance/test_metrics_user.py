"""metrics 计量层用户级测试：user_open_id 落库 + 用户用量归属 + 双写 governor。"""

from __future__ import annotations

import sqlite3

from smartclaw.config.loader import Config, GovernanceConfig
from smartclaw.governance.governor import TenantGovernor
from smartclaw.governance.store import InMemoryStore
from smartclaw.monitoring import metrics


def test_tracker_writes_user_open_id_column(tmp_path):
    db = tmp_path / "tokens.db"
    tracker = metrics.TokenUsageTracker(db_path=db)
    tracker.record(
        agent_id="bot",
        provider="deepseek",
        model="deepseek-chat",
        prompt_tokens=10,
        completion_tokens=20,
        latency_ms=5,
        tenant_id="acme",
        user_open_id="ou_zhang",
    )
    conn = sqlite3.connect(db)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(token_usage)")}
    assert "user_open_id" in cols
    row = conn.execute(
        "SELECT tenant_id, user_open_id, total_tokens FROM token_usage"
    ).fetchone()
    conn.close()
    assert row == ("acme", "ou_zhang", 30)


def test_get_stats_filter_by_user(tmp_path):
    tracker = metrics.TokenUsageTracker(db_path=tmp_path / "tokens.db")
    common = dict(agent_id="bot", provider="deepseek", model="m", latency_ms=1, tenant_id="acme")
    tracker.record(prompt_tokens=10, completion_tokens=0, user_open_id="ou_a", **common)
    tracker.record(prompt_tokens=20, completion_tokens=0, user_open_id="ou_a", **common)
    tracker.record(prompt_tokens=5, completion_tokens=0, user_open_id="ou_b", **common)

    stats_a = tracker.get_stats(user_open_id="ou_a")
    stats_b = tracker.get_stats(user_open_id="ou_b")
    assert stats_a["total_tokens"] == 30
    assert stats_a["request_count"] == 2
    assert stats_b["total_tokens"] == 5
    # 不传过滤 → 全量
    assert tracker.get_stats()["total_tokens"] == 35


def test_record_token_usage_dual_records_tenant_and_user(tmp_path, monkeypatch):
    tracker = metrics.TokenUsageTracker(db_path=tmp_path / "tokens.db")
    monkeypatch.setattr(metrics, "_global_tracker", tracker)

    cfg = Config(
        governance=GovernanceConfig(
            enabled=True,
            default_daily_token_quota=1000,
            default_user_daily_token_quota=500,
        )
    )
    gov = TenantGovernor(store=InMemoryStore(), config_provider=lambda: cfg)
    import smartclaw.governance as governance_pkg

    monkeypatch.setattr(governance_pkg, "get_governor", lambda: gov)

    metrics.record_token_usage(
        agent_id="bot",
        provider="deepseek",
        model="deepseek-chat",
        prompt_tokens=40,
        completion_tokens=60,
        latency_ms=12,
        tenant_id="acme",
        user_open_id="ou_zhang",
    )
    # 租户与用户两个维度都被累计
    assert gov.snapshot("acme")["used_today"] == 100
    assert gov.user_snapshot("acme", "ou_zhang")["used_today"] == 100


def test_record_token_usage_without_user_only_tenant(tmp_path, monkeypatch):
    """不传 user_open_id（旧调用方）→ 仅租户累计，用户维度为 0，行为兼容。"""
    tracker = metrics.TokenUsageTracker(db_path=tmp_path / "tokens.db")
    monkeypatch.setattr(metrics, "_global_tracker", tracker)

    cfg = Config(
        governance=GovernanceConfig(
            enabled=True,
            default_daily_token_quota=1000,
            default_user_daily_token_quota=500,
        )
    )
    gov = TenantGovernor(store=InMemoryStore(), config_provider=lambda: cfg)
    import smartclaw.governance as governance_pkg

    monkeypatch.setattr(governance_pkg, "get_governor", lambda: gov)

    metrics.record_token_usage(
        agent_id="bot",
        provider="openai",
        model="gpt-4",
        prompt_tokens=10,
        completion_tokens=10,
        latency_ms=1,
        tenant_id="acme",
    )
    assert gov.snapshot("acme")["used_today"] == 20
    assert gov.user_snapshot("acme", "ou_any")["used_today"] == 0
    # 落库 user_open_id 为空串
    conn = sqlite3.connect(tmp_path / "tokens.db")
    val = conn.execute("SELECT user_open_id FROM token_usage").fetchone()[0]
    conn.close()
    assert val == ""
