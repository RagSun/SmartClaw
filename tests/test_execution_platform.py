"""平台鉴权与租户记忆隔离回归测试。"""

import tempfile
from pathlib import Path

from smartclaw.auth.platform import PlatformAuthAdapter
from smartclaw.config.loader import Config, PlatformAuthConfig
from smartclaw.memory.manager import MemoryManager
from smartclaw.memory.storage.sqlite_store import SQLiteStore
from smartclaw.monitoring.execution_trace import record_execution_event
from smartclaw.monitoring.metrics import get_execution_counters, record_execution_path_event


def test_platform_auth_monitoring_optional():
    cfg = Config(
        auth=PlatformAuthConfig(monitoring_require_auth=False, monitoring_bearer_token=""),
    )
    assert PlatformAuthAdapter.verify_monitoring_bearer(None, cfg) is True
    assert PlatformAuthAdapter.verify_monitoring_bearer("Bearer secret", cfg) is True

    cfg2 = Config(
        auth=PlatformAuthConfig(monitoring_require_auth=True, monitoring_bearer_token="tok"),
    )
    assert PlatformAuthAdapter.verify_monitoring_bearer("Bearer tok", cfg2) is True
    assert PlatformAuthAdapter.verify_monitoring_bearer("Bearer wrong", cfg2) is False


def test_platform_auth_feishu_secret():
    cfg = Config(auth=PlatformAuthConfig(feishu_webhook_secret=""))
    assert PlatformAuthAdapter.verify_feishu_webhook(None, None, cfg) is True

    cfg2 = Config(auth=PlatformAuthConfig(feishu_webhook_secret="abc"))
    assert PlatformAuthAdapter.verify_feishu_webhook("abc", None, cfg2) is True
    assert PlatformAuthAdapter.verify_feishu_webhook(None, "abc", cfg2) is True
    assert PlatformAuthAdapter.verify_feishu_webhook("x", None, cfg2) is False


def test_execution_trace_no_throw():
    record_execution_event(
        event_type="unit_test",
        trace_id="t1",
        agent_id="a",
        session_id="s",
        tenant_id="default",
        data={"k": 1},
        emit=False,
    )


def test_execution_counters_increment():
    before = get_execution_counters().get("planner_ok", 0)
    record_execution_path_event("planner_ok")
    after = get_execution_counters().get("planner_ok", 0)
    assert after == before + 1


def test_sqlite_get_messages_returns_recent_chronological():
    """滑动窗口：应取最近 N 条，且按时间正序（与旧版「最旧 N 条」语义相反）。"""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "m.db"
        store = SQLiteStore(db)
        store.initialize()
        sid = "session-1"
        for i in range(5):
            store.add_message(sid, "user", f"msg-{i}", tenant_id="default")
        recent = store.get_messages(sid, limit=3, tenant_id="default")
        assert len(recent) == 3
        assert [r["content"] for r in recent] == ["msg-2", "msg-3", "msg-4"]
        store.close()


def test_sqlite_tenant_isolation_same_session_id():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "m.db"
        store = SQLiteStore(db)
        store.initialize()
        sid = "session-1"
        store.add_message(sid, "user", "hi-a", tenant_id="tenant-a")
        store.add_message(sid, "user", "hi-b", tenant_id="tenant-b")
        a_msgs = store.get_messages(sid, tenant_id="tenant-a")
        b_msgs = store.get_messages(sid, tenant_id="tenant-b")
        assert len(a_msgs) == 1
        assert len(b_msgs) == 1
        assert b_msgs[0]["content"] == "hi-b"
        store.close()


def test_memory_manager_respects_tenant():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        m1 = MemoryManager(
            agent_id="same",
            session_id="s1",
            channel="feishu",
            user_id="u1",
            data_dir=base,
        )
        m1.tenant_id = "t1"
        m1.add_message("user", "only-t1")

        m2 = MemoryManager(
            agent_id="same",
            session_id="s1",
            channel="feishu",
            user_id="u1",
            data_dir=base,
        )
        m2.tenant_id = "t2"
        m2.add_message("user", "only-t2")

        c1 = m1._store.get_message_count("s1", tenant_id="t1")
        c2 = m2._store.get_message_count("s1", tenant_id="t2")
        assert c1 == 1
        assert c2 == 1

        m_wrong = MemoryManager(
            agent_id="same",
            session_id="s1",
            channel="feishu",
            user_id="u1",
            data_dir=base,
        )
        m_wrong.tenant_id = "t1"
        ctx = m_wrong.get_context_for_llm(max_messages=10, include_stored_transcript=True)
        assert any("only-t1" in (m.get("content") or "") for m in ctx)
        m1.close()
        m2.close()
        m_wrong.close()


def test_auth_policy_manager_resolve_tenant():
    from smartclaw.auth.policy_manager import AuthPolicyManager

    cfg = Config(
        auth=PlatformAuthConfig(
            tenant_default="default",
            tenant_by_app_id={"app_x": "tenant-a"},
        )
    )
    assert AuthPolicyManager.resolve_tenant_for_feishu("app_x", cfg) == "tenant-a"
    assert AuthPolicyManager.resolve_tenant_for_feishu("other", cfg) == "default"


def test_auth_policy_manager_declared_tenant():
    from smartclaw.auth.policy_manager import AuthPolicyManager

    cfg = Config(auth=PlatformAuthConfig(tenant_trust_header=True, tenant_default="t1"))
    assert AuthPolicyManager.verify_declared_tenant("t1", "t1", cfg)[0] is True
    assert AuthPolicyManager.verify_declared_tenant("t2", "t1", cfg)[0] is False
    assert AuthPolicyManager.verify_declared_tenant(None, "t1", cfg)[0] is True


def test_monitoring_jwt_hs256():
    import jwt

    cfg = Config(
        auth=PlatformAuthConfig(
            monitoring_require_auth=True,
            monitoring_jwt_enabled=True,
            monitoring_jwt_secret="secret",
            monitoring_jwt_algorithm="HS256",
        )
    )
    token = jwt.encode({"sub": "tester"}, "secret", algorithm="HS256")
    assert PlatformAuthAdapter.verify_monitoring_jwt(f"Bearer {token}", cfg) is True
    assert PlatformAuthAdapter.verify_monitoring_jwt("Bearer bad", cfg) is False


def test_webhook_replay_guard():
    from smartclaw.auth.webhook_replay import WebhookReplayGuard

    g = WebhookReplayGuard(120)
    assert g.is_replay("evt-1") is False
    assert g.is_replay("evt-1") is True


def test_parse_feishu_event_minimal():
    from smartclaw.channel.feishu_context import parse_feishu_event_body

    body = {
        "schema": "2.0",
        "header": {"event_id": "e1"},
        "event": {
            "message": {
                "chat_id": "oc_1",
                "chat_type": "group",
                "content": '{"text":"hi @default"}',
                "message_id": "m1",
            },
            "sender": {"sender_id": {"open_id": "ou_u1"}},
        },
    }
    ctx = parse_feishu_event_body(body)
    assert ctx is not None
    assert ctx.is_group is True
    assert ctx.user_open_id == "ou_u1"


def test_lark_request_signature_roundtrip():
    from smartclaw.auth.feishu_payload import (
        compute_lark_request_signature,
        verify_lark_request_signature,
        verify_lark_request_signature_try_keys,
    )

    raw = b'{"schema":"2.0"}'
    ts, nonce, key = "1234567890", "nonce1", "my_encrypt_key"
    sig = compute_lark_request_signature(raw, ts, nonce, key)
    assert verify_lark_request_signature(raw, ts, nonce, sig, key) is True
    assert verify_lark_request_signature(raw, ts, nonce, "wrong", key) is False
    assert verify_lark_request_signature_try_keys(raw, ts, nonce, sig, ["other", key]) is True


def test_platform_verify_lark_signature_optional():
    from smartclaw.auth.platform import PlatformAuthAdapter

    ok, reason = PlatformAuthAdapter.verify_lark_webhook_signature_if_present(b"{}", {}, ["k"])
    assert ok is True and reason == ""

    hdr = {
        "X-Lark-Signature": "a",
        "X-Lark-Request-Timestamp": "1",
        "X-Lark-Request-Nonce": "n",
    }
    ok, reason = PlatformAuthAdapter.verify_lark_webhook_signature_if_present(b"{}", hdr, [])
    assert ok is False
    assert "encrypt_key" in reason
