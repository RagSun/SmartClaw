"""metrics 计量层多租户测试：tenant_id 落库 + 通知 governor 累计配额。"""

from __future__ import annotations

import sqlite3

from smartclaw.config.loader import Config, GovernanceConfig
from smartclaw.governance.governor import TenantGovernor
from smartclaw.governance.store import InMemoryStore
from smartclaw.monitoring import metrics


def test_tracker_writes_tenant_id_column(tmp_path):
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
    )
    conn = sqlite3.connect(db)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(token_usage)")}
    assert "tenant_id" in cols
    row = conn.execute(
        "SELECT tenant_id, total_tokens FROM token_usage"
    ).fetchone()
    conn.close()
    assert row == ("acme", 30)


def test_record_token_usage_notifies_governor(tmp_path, monkeypatch):
    # 用临时 db 的 tracker 替换全局，避免落到真实路径
    tracker = metrics.TokenUsageTracker(db_path=tmp_path / "tokens.db")
    monkeypatch.setattr(metrics, "_global_tracker", tracker)

    cfg = Config(governance=GovernanceConfig(enabled=True, default_daily_token_quota=1000))
    gov = TenantGovernor(store=InMemoryStore(), config_provider=lambda: cfg)
    # record_token_usage 内部 `from smartclaw.governance import get_governor`，故打桩该属性
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
    )
    assert gov.snapshot("acme")["used_today"] == 100


def test_record_token_usage_backward_compatible_without_tenant(tmp_path, monkeypatch):
    """旧调用方不传 tenant_id 时仍可用，落库为 default。"""
    tracker = metrics.TokenUsageTracker(db_path=tmp_path / "tokens.db")
    monkeypatch.setattr(metrics, "_global_tracker", tracker)
    rid = metrics.record_token_usage(
        agent_id="bot",
        provider="openai",
        model="gpt-4",
        prompt_tokens=1,
        completion_tokens=1,
        latency_ms=1,
    )
    assert isinstance(rid, int)
    conn = sqlite3.connect(tmp_path / "tokens.db")
    val = conn.execute("SELECT tenant_id FROM token_usage").fetchone()[0]
    conn.close()
    assert val == "default"
