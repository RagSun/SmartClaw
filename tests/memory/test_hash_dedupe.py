"""哈希去重工具与 SQLite memory_notes 行为。"""

import tempfile
from pathlib import Path

from smartclaw.memory.hash_dedupe import (
    append_hash,
    load_hash_set,
    note_row_hash,
    promote_entry_hash,
)
from smartclaw.memory.storage.sqlite_store import SQLiteStore


def test_note_row_hash_collapses_whitespace() -> None:
    a = note_row_hash(
        note_kind="用户偏好",
        content="记住我  喜欢深色",
        user_id="u1",
        agent_id="a1",
    )
    b = note_row_hash(
        note_kind="用户偏好",
        content="记住我 喜欢深色",
        user_id="u1",
        agent_id="a1",
    )
    assert a == b


def test_promote_entry_hash_distinct_users() -> None:
    h1 = promote_entry_hash(
        user_id="u1", agent_id="a", note_kind="t", content="x"
    )
    h2 = promote_entry_hash(
        user_id="u2", agent_id="a", note_kind="t", content="x"
    )
    assert h1 != h2


def test_load_append_hash_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "h.txt"
    append_hash(p, "abc")
    append_hash(p, "def")
    s = load_hash_set(p)
    assert s == {"abc", "def"}


def test_sqlite_add_memory_note_dedupe_by_hash() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "m.db"
        store = SQLiteStore(db)
        store.initialize()
        r1 = store.add_memory_note(
            "用户偏好",
            "记住我  喜欢测试",
            importance=8,
            user_id="u",
            agent_id="a",
            dedupe=True,
        )
        assert r1 > 0
        r2 = store.add_memory_note(
            "用户偏好",
            "记住我 喜欢测试",
            importance=8,
            user_id="u",
            agent_id="a",
            dedupe=True,
        )
        assert r2 == 0
        store.close()
