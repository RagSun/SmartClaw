"""memory_notes / user_profile 的租户纵深防御 + 旧库在线迁移测试。

验证数据库层强制带 tenant 条件：即便同一个库文件里混入多个租户的数据，查询也
只返回本租户；并覆盖旧库在线迁移（events→memory_notes / event_type→note_kind、
补 tenant_id 列、FTS 触发器升级、embeddings.source_kind 'event'→'note'）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from smartclaw.memory.storage.sqlite_store import SQLiteStore


def _store(tmp_path: Path) -> SQLiteStore:
    s = SQLiteStore(tmp_path / "mem.db")
    s.initialize()
    return s


# ----- memory_notes 按租户隔离 -----

def test_get_memory_notes_filters_by_tenant(tmp_path):
    s = _store(tmp_path)
    try:
        s.add_memory_note("note", "acme 的要点", user_id="u", agent_id="a", tenant_id="acme")
        s.add_memory_note("note", "globex 的要点", user_id="u", agent_id="a", tenant_id="globex")
        acme = s.get_memory_notes(user_id="u", agent_id="a", tenant_id="acme")
        globex = s.get_memory_notes(user_id="u", agent_id="a", tenant_id="globex")
        assert [e["content"] for e in acme] == ["acme 的要点"]
        assert [e["content"] for e in globex] == ["globex 的要点"]
        # 未指定 → 默认租户，看不到上述任一
        assert s.get_memory_notes(user_id="u", agent_id="a") == []
    finally:
        s.close()


def test_note_dedupe_is_per_tenant(tmp_path):
    s = _store(tmp_path)
    try:
        # 同样内容，不同租户：dedupe 不应跨租户误判，二者都应写入
        r1 = s.add_memory_note("note", "同样的内容", user_id="u", agent_id="a", dedupe=True, tenant_id="acme")
        r2 = s.add_memory_note("note", "同样的内容", user_id="u", agent_id="a", dedupe=True, tenant_id="globex")
        assert r1 and r2 and r1 != r2
        # 同租户重复 → 去重跳过
        r3 = s.add_memory_note("note", "同样的内容", user_id="u", agent_id="a", dedupe=True, tenant_id="acme")
        assert r3 == 0
    finally:
        s.close()


def test_get_memory_record_note_respects_tenant(tmp_path):
    s = _store(tmp_path)
    try:
        nid = s.add_memory_note("note", "acme 要点", user_id="u", agent_id="a", tenant_id="acme")
        # 正确租户能取到
        rec = s.get_memory_record(source_kind="note", source_id=str(nid), tenant_id="acme", user_id="u", agent_id="a")
        assert rec and rec["body"] == "acme 要点"
        # 错误租户取不到
        none = s.get_memory_record(source_kind="note", source_id=str(nid), tenant_id="globex", user_id="u", agent_id="a")
        assert none is None
    finally:
        s.close()


# ----- user_profile 按租户隔离 -----

def test_profile_filters_by_tenant(tmp_path):
    s = _store(tmp_path)
    try:
        s.set_profile("u", "a", "lang", "zh", tenant_id="acme")
        assert s.get_profile("u", "a", tenant_id="acme") == {"lang": "zh"}
        # 其他租户读不到
        assert s.get_profile("u", "a", tenant_id="globex") == {}
        # 默认租户读不到
        assert s.get_profile("u", "a") == {}
    finally:
        s.close()


def test_profile_same_key_across_tenants_no_overwrite(tmp_path):
    """回归：同一 (user_id, agent_id, key) 在不同租户下必须各自独立，
    后写入者不得通过 ON CONFLICT 覆盖前者（旧唯一约束缺 tenant_id 的硬伤）。"""
    s = _store(tmp_path)
    try:
        s.set_profile("u", "a", "secret", "A-only", tenant_id="acme")
        s.set_profile("u", "a", "secret", "B-only", tenant_id="globex")
        assert s.get_profile("u", "a", tenant_id="acme") == {"secret": "A-only"}
        assert s.get_profile("u", "a", tenant_id="globex") == {"secret": "B-only"}
        # 同租户同键 → 正常 upsert 覆盖自身
        s.set_profile("u", "a", "secret", "A-new", tenant_id="acme")
        assert s.get_profile("u", "a", tenant_id="acme") == {"secret": "A-new"}
        assert s.get_profile("u", "a", tenant_id="globex") == {"secret": "B-only"}
    finally:
        s.close()


def test_migration_user_profile_unique_includes_tenant(tmp_path):
    """旧库 user_profile 唯一约束缺 tenant_id，打开后应被重建为含 tenant_id，
    且既有数据保留、跨租户同键可共存。"""
    db = tmp_path / "legacy_profile.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE user_profile (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            confidence INTEGER DEFAULT 5,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, agent_id, key)
        )
        """
    )
    conn.execute(
        "INSERT INTO user_profile (user_id, agent_id, key, value) VALUES (?,?,?,?)",
        ("u", "a", "lang", "zh"),
    )
    conn.commit()
    conn.close()

    s = SQLiteStore(db)
    s.initialize()
    try:
        # 唯一索引必须已含 tenant_id
        idx_cols = set()
        c = s._get_connection()
        for row in c.execute("PRAGMA index_list(user_profile)").fetchall():
            if row[2]:  # unique
                for ir in c.execute(f"PRAGMA index_info('{row[1]}')").fetchall():
                    idx_cols.add(ir[2])
        assert "tenant_id" in idx_cols
        # 历史行回填为 default 租户，仍可读
        assert s.get_profile("u", "a", tenant_id="default") == {"lang": "zh"}
        # 现在跨租户同键可共存
        s.set_profile("u", "a", "lang", "en", tenant_id="acme")
        assert s.get_profile("u", "a", tenant_id="default") == {"lang": "zh"}
        assert s.get_profile("u", "a", tenant_id="acme") == {"lang": "en"}
    finally:
        s.close()


# ----- FTS 记忆要点检索按租户隔离 -----

def test_fts_note_search_filters_by_tenant(tmp_path):
    s = _store(tmp_path)
    try:
        if not s._fts_ready:
            return
        s.add_memory_note("note", "alpha beta sqlite gamma", user_id="u", agent_id="a", tenant_id="acme")
        hits_acme = s.search_memory_fts(match_query="sqlite", session_id="x", tenant_id="acme", user_id="u")
        hits_other = s.search_memory_fts(match_query="sqlite", session_id="x", tenant_id="globex", user_id="u")
        assert any("sqlite" in (h.get("body") or "") for h in hits_acme)
        assert hits_other == []
    finally:
        s.close()


def test_fresh_note_trigger_uses_new_tenant(tmp_path):
    s = _store(tmp_path)
    try:
        conn = s._get_connection()
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='memory_fts_ai_note'"
        ).fetchone()
        if row is None:
            return  # 该平台未启用 FTS
        assert "NEW.tenant_id" in row[0]
        # 旧 events 触发器不得遗留
        old = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='memory_fts_ai_ev'"
        ).fetchone()
        assert old is None
    finally:
        s.close()


# ----- 旧库在线迁移：events→memory_notes / event_type→note_kind -----

def test_migration_events_to_memory_notes(tmp_path):
    """技术债清除回归：旧库只有 events/event_type（且缺 tenant_id 列），打开后应被
    在线改名为 memory_notes/note_kind、补 tenant_id、历史数据可经新 API 读取。"""
    db = tmp_path / "legacy_events.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            content TEXT NOT NULL,
            importance INTEGER DEFAULT 5,
            tags TEXT,
            user_id TEXT,
            agent_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "INSERT INTO events (event_type, content, user_id, agent_id) VALUES (?,?,?,?)",
        ("用户偏好", "历史要点", "u", "a"),
    )
    conn.commit()
    conn.close()

    s = SQLiteStore(db)
    s.initialize()
    try:
        c = s._get_connection()
        tables = {
            r[0]
            for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        # 旧表已改名，新表存在
        assert "memory_notes" in tables
        assert "events" not in tables
        cols = {r[1] for r in c.execute("PRAGMA table_info(memory_notes)").fetchall()}
        assert "note_kind" in cols and "event_type" not in cols
        assert "tenant_id" in cols
        # 历史行按默认值 'default' 回填，经新 API 可读，且 note_kind 保留原值
        ns = s.get_memory_notes(user_id="u", agent_id="a", tenant_id="default")
        assert [(n["content"], n["note_kind"]) for n in ns] == [("历史要点", "用户偏好")]
    finally:
        s.close()


def test_migration_events_to_memory_notes_is_idempotent(tmp_path):
    """重复打开同一已迁移库不应报错、不重复迁移、数据稳定（幂等）。"""
    db = tmp_path / "legacy_idem.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            content TEXT NOT NULL,
            importance INTEGER DEFAULT 5,
            tags TEXT,
            user_id TEXT,
            agent_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "INSERT INTO events (event_type, content, user_id, agent_id) VALUES (?,?,?,?)",
        ("milestone", "里程碑要点 needle", "u", "a"),
    )
    conn.commit()
    conn.close()

    # 连开两次
    for _ in range(2):
        s = SQLiteStore(db)
        s.initialize()
        s.close()

    s = SQLiteStore(db)
    s.initialize()
    try:
        ns = s.get_memory_notes(user_id="u", agent_id="a", tenant_id="default")
        assert [n["content"] for n in ns] == ["里程碑要点 needle"]
        if s._fts_ready:
            # 旧 FTS 行（kind='event'）应已收敛为 'note'，新 API 可召回
            hits = s.search_memory_fts(
                match_query="needle", session_id="x", tenant_id="default", user_id="u"
            )
            assert any("needle" in (h.get("body") or "") for h in hits)
            # 不得遗留 kind='event' 的 FTS 行
            leftover = s._get_connection().execute(
                "SELECT COUNT(*) FROM memory_fts WHERE kind='event'"
            ).fetchone()[0]
            assert leftover == 0
    finally:
        s.close()
