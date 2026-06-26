"""PostgresStore 真实 PostgreSQL 测试（记忆数据面水平扩展，见 progress.md §12.2）。

无 mock、无回退：未设 ``SMARTCLAW_TEST_POSTGRES_DSN`` 或 PG 不可达 → friendly skip。
关键证据：
- ``test_tenant_isolation_*``：同 session/key 跨租户互不串（合库后应用层强制过滤）。
- ``test_agent_isolation``：合库后按 agent_id 物理隔离不丢失。
- ``test_session_lock_serializes_same_session``：多「实例」（多连接/多线程）并发写同一会话
  被 advisory 锁串行化——这是本地 SQLite 多实例做不到的一致性保证。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
from _postgres_real import cleanup, dsn_or_skip, new_agent  # noqa: E402


@pytest.fixture()
def pg():
    dsn = dsn_or_skip()
    from smartclaw.memory.storage.postgres_store import PostgresStore

    made: list = []

    def make(agent_id: str | None = None) -> "PostgresStore":  # noqa: F821
        st = PostgresStore(dsn=dsn, agent_id=agent_id or new_agent())
        st.initialize()
        made.append(st)
        return st

    try:
        yield make
    finally:
        for st in made:
            st.close()
        cleanup(dsn)


# --------------------------------------------------------------------------- #
def test_messages_roundtrip_and_order(pg):
    st = pg()
    sid = "sess-1"
    a = st.add_message(sid, "user", "你好，第一条", tenant_id="t1")
    b = st.add_message(sid, "assistant", "收到，第二条", tenant_id="t1")
    assert a > 0 and b > a
    assert st.get_message_count(sid, tenant_id="t1") == 2
    msgs = st.get_messages(sid, tenant_id="t1")
    assert [m["content"] for m in msgs] == ["你好，第一条", "收到，第二条"]
    assert all("created_at" in m for m in msgs)


def test_summaries(pg):
    st = pg()
    sid = "sess-sum"
    st.add_message(sid, "user", "x", tenant_id="t1")
    assert st.get_latest_summary(sid, tenant_id="t1") is None
    st.add_summary(sid, "这是摘要", original_count=1, tenant_id="t1")
    latest = st.get_latest_summary(sid, tenant_id="t1")
    assert latest and latest["summary"] == "这是摘要"
    assert len(st.get_summaries(sid, tenant_id="t1")) == 1


def test_tenant_isolation_messages(pg):
    st = pg()
    sid = "shared-session"
    st.add_message(sid, "user", "租户A的消息", tenant_id="tA")
    st.add_message(sid, "user", "租户B的消息1", tenant_id="tB")
    st.add_message(sid, "user", "租户B的消息2", tenant_id="tB")
    assert st.get_message_count(sid, tenant_id="tA") == 1
    assert st.get_message_count(sid, tenant_id="tB") == 2
    assert [m["content"] for m in st.get_messages(sid, tenant_id="tA")] == ["租户A的消息"]


def test_tenant_isolation_profile_no_crosswrite(pg):
    st = pg()
    st.set_profile("u1", st.agent_id, "city", "上海", tenant_id="tA")
    st.set_profile("u1", st.agent_id, "city", "北京", tenant_id="tB")
    assert st.get_profile("u1", st.agent_id, tenant_id="tA") == {"city": "上海"}
    assert st.get_profile("u1", st.agent_id, tenant_id="tB") == {"city": "北京"}


def test_agent_isolation(pg):
    a1 = pg()
    a2 = pg()
    sid = "same-session"
    a1.add_message(sid, "user", "agent1 的消息", tenant_id="t1")
    a2.add_message(sid, "user", "agent2 的消息", tenant_id="t1")
    assert a1.get_message_count(sid, tenant_id="t1") == 1
    assert [m["content"] for m in a1.get_messages(sid, tenant_id="t1")] == ["agent1 的消息"]
    assert [m["content"] for m in a2.get_messages(sid, tenant_id="t1")] == ["agent2 的消息"]


def test_memory_notes_dedupe_and_filter(pg):
    st = pg()
    first = st.add_memory_note("重要记录", "客户要求周五交付", user_id="u1",
                         agent_id=st.agent_id, dedupe=True, tenant_id="t1")
    again = st.add_memory_note("重要记录", "客户要求周五交付", user_id="u1",
                         agent_id=st.agent_id, dedupe=True, tenant_id="t1")
    assert first > 0 and again == 0  # 去重命中
    # 跨租户同内容不算重复
    other = st.add_memory_note("重要记录", "客户要求周五交付", user_id="u1",
                         agent_id=st.agent_id, dedupe=True, tenant_id="t2")
    assert other > 0
    notes = st.get_memory_notes(user_id="u1", agent_id=st.agent_id, tenant_id="t1")
    assert len(notes) == 1 and notes[0]["content"] == "客户要求周五交付"


def test_profile_upsert(pg):
    st = pg()
    st.set_profile("u1", st.agent_id, "role", "工程师", tenant_id="t1")
    st.set_profile("u1", st.agent_id, "role", "架构师", confidence=9, tenant_id="t1")
    assert st.get_profile("u1", st.agent_id, tenant_id="t1") == {"role": "架构师"}


def test_embedding_upsert_get(pg):
    st = pg()
    mid = st.add_message("s", "user", "向量化内容", tenant_id="t1")
    st.upsert_embedding(
        source_kind="message", source_id=str(mid), tenant_id="t1", user_id="u1",
        agent_id=st.agent_id, content="向量化内容", embedding_model="m1",
        vector=[0.1, 0.2, 0.3],
    )
    got = st.get_embedding(source_kind="message", source_id=str(mid), embedding_model="m1")
    assert got and got["dimensions"] == 3 and got["tenant_id"] == "t1"


def test_fts_search_substring(pg):
    st = pg()
    sid = "fts-session"
    st.add_message(sid, "user", "我们计划在杭州建一个新的电芯车间", tenant_id="t1")
    st.add_message(sid, "assistant", "好的，杭州车间预算多少", tenant_id="t1")
    st.add_message(sid, "user", "完全无关的一句话", tenant_id="t1")
    hits = st.search_memory_fts(
        raw_query="杭州车间", session_id=sid, tenant_id="t1", user_id="u1", limit=5
    )
    bodies = " ".join(h["body"] for h in hits)
    assert hits and "杭州" in bodies
    # 租户隔离：另一租户搜不到
    assert st.search_memory_fts(
        raw_query="杭州车间", session_id=sid, tenant_id="t2", user_id="u1", limit=5
    ) == []


def test_session_lock_serializes_same_session(pg):
    st = pg()
    order: list[str] = []

    def worker(tag: str, delay: float) -> None:
        time.sleep(delay)
        with st.session_lock("t1", "lock-session"):
            order.append(f"{tag}-start")
            time.sleep(0.4)
            order.append(f"{tag}-end")

    t1 = threading.Thread(target=worker, args=("A", 0.0))
    t2 = threading.Thread(target=worker, args=("B", 0.1))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    # 不允许交叉：每个 start 紧跟自己的 end
    assert len(order) == 4, order
    assert order[0].endswith("-start") and order[1] == order[0].replace("-start", "-end")
    assert order[2].endswith("-start") and order[3] == order[2].replace("-start", "-end")


def test_session_lock_allows_different_sessions_parallel(pg):
    st = pg()
    timeline: list[tuple[str, float]] = []

    def worker(session: str) -> None:
        with st.session_lock("t1", session):
            timeline.append((f"{session}-start", time.monotonic()))
            time.sleep(0.4)
            timeline.append((f"{session}-end", time.monotonic()))

    t1 = threading.Thread(target=worker, args=("sess-A",))
    t2 = threading.Thread(target=worker, args=("sess-B",))
    start = time.monotonic()
    t1.start(); t2.start(); t1.join(10); t2.join(10)
    # 不同会话应并行：总耗时显著小于 2*0.4
    assert (time.monotonic() - start) < 0.7, timeline


def test_factory_selects_postgres(pg):
    dsn = dsn_or_skip()
    from smartclaw.memory.storage.factory import create_memory_store
    from smartclaw.memory.storage.postgres_store import PostgresStore

    st = create_memory_store(
        agent_id=new_agent(), memory_subdir=Path("."), store="postgres", postgres_dsn=dsn
    )
    try:
        assert isinstance(st, PostgresStore)
    finally:
        st.close()
        cleanup(dsn)
