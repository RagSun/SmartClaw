"""SQLite 记忆存储层"""

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

from smartclaw.console import debug
from smartclaw.logging_utils import safe_preview
from smartclaw.memory.hash_dedupe import note_row_hash


def _log_preview(text: str, max_len: int = 96) -> str:
    return safe_preview(text or "", max_len)


class SQLiteStore:
    def __init__(self, db_path: Path, max_size_mb: int = 100):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_size_mb = max_size_mb
        self._conn: Optional[sqlite3.Connection] = None
        self._fts_ready: bool = False

    def _get_connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                timeout=30.0,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def initialize(self) -> None:
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                tokens INTEGER DEFAULT 0,
                tool_name TEXT,
                tool_call_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                summary TEXT NOT NULL,
                original_count INTEGER DEFAULT 0,
                summary_type TEXT DEFAULT 'auto',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 记忆要点（memory_notes）：从对话抽取的结构化要点（偏好/决策/禁止等）。
        # 历史名为 events / event_type，已统一改为 memory_notes / note_kind 以与 EventBus、
        # 执行埋点等「事件」彻底区分（迁移见 _migrate_events_to_memory_notes）。
        self._migrate_events_to_memory_notes(conn)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memory_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                note_kind TEXT NOT NULL,
                content TEXT NOT NULL,
                importance INTEGER DEFAULT 5,
                tags TEXT,
                user_id TEXT,
                agent_id TEXT,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_profile (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                confidence INTEGER DEFAULT 5,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(tenant_id, user_id, agent_id, key)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memory_embeddings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_kind TEXT NOT NULL,
                source_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                user_id TEXT,
                agent_id TEXT,
                content_hash TEXT NOT NULL,
                embedding_model TEXT NOT NULL,
                dimensions INTEGER DEFAULT 0,
                embedding_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source_kind, source_id, embedding_model)
            )
        """)

        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_session "
            "ON messages(session_id, created_at)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_notes_user "
            "ON memory_notes(user_id, agent_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_notes_kind "
            "ON memory_notes(note_kind)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_embeddings_scope "
            "ON memory_embeddings(tenant_id, user_id, agent_id, embedding_model)"
        )

        self._migrate_tenant_columns(conn)
        self._migrate_user_profile_unique(conn)
        self._migrate_note_dedupe_hash(conn)
        self._fts_ready = self._migrate_memory_fts(conn)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_tenant_session "
            "ON messages(tenant_id, session_id, created_at)"
        )

        conn.commit()

    def _migrate_tenant_columns(self, conn: sqlite3.Connection) -> None:
        """为核心业务表补 tenant_id 列（在线 ALTER，兼容旧库）。

        覆盖 messages / summaries / memory_notes / user_profile 四张表，实现数据库层的
        租户纵深防御：即便目录路径拼错，查询仍强制带 tenant 条件而不致串库。
        旧库已有数据按列默认值 'default' 回填——这与「目录已按租户隔离、非默认租户
        为全新数据」一致，默认租户历史数据照常匹配。
        """
        cursor = conn.cursor()
        for table in ("messages", "summaries", "memory_notes", "user_profile"):
            cursor.execute(f"PRAGMA table_info({table})")
            cols = {row[1] for row in cursor.fetchall()}
            if "tenant_id" not in cols:
                cursor.execute(
                    f"ALTER TABLE {table} ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'"
                )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_notes_tenant_user "
            "ON memory_notes(tenant_id, user_id, agent_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_profile_tenant "
            "ON user_profile(tenant_id, user_id, agent_id)"
        )

    def _migrate_user_profile_unique(self, conn: sqlite3.Connection) -> None:
        """修正 user_profile 唯一约束，使其包含 tenant_id（纵深防御关键项）。

        旧库为 ``UNIQUE(user_id, agent_id, key)``，未含租户：当两个租户恰好有
        相同 ``(user_id, agent_id, key)`` 时，后写入者会通过 ``ON CONFLICT``
        覆盖前者那行（连带改写其 tenant_id），造成**跨租户画像互相覆盖/丢失**。
        本迁移检测旧约束并在线重建表为 ``UNIQUE(tenant_id, user_id, agent_id, key)``，
        既有数据原样搬迁（旧约束保证不会有冲突行），对新库为无操作。
        """
        cursor = conn.cursor()
        cursor.execute("PRAGMA index_list(user_profile)")
        has_tenant_unique = False
        for row in cursor.fetchall():
            name, is_unique = row[1], row[2]
            if not is_unique:
                continue
            cursor.execute(f"PRAGMA index_info('{name}')")
            cols = {r[2] for r in cursor.fetchall()}
            if "tenant_id" in cols and {"user_id", "agent_id", "key"}.issubset(cols):
                has_tenant_unique = True
                break
        if has_tenant_unique:
            return
        # 旧库：重建表，把唯一约束升级为含 tenant_id（避免跨租户覆盖）。
        cursor.execute("ALTER TABLE user_profile RENAME TO user_profile_legacy")
        cursor.execute("""
            CREATE TABLE user_profile (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                confidence INTEGER DEFAULT 5,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(tenant_id, user_id, agent_id, key)
            )
        """)
        cursor.execute("""
            INSERT INTO user_profile
                (id, user_id, agent_id, tenant_id, key, value, confidence, updated_at)
            SELECT id, user_id, agent_id, tenant_id, key, value, confidence, updated_at
            FROM user_profile_legacy
        """)
        cursor.execute("DROP TABLE user_profile_legacy")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_profile_tenant "
            "ON user_profile(tenant_id, user_id, agent_id)"
        )

    def _migrate_events_to_memory_notes(self, conn: sqlite3.Connection) -> None:
        """技术债清除：``events`` → ``memory_notes``、``event_type`` → ``note_kind`` 的一次性幂等迁移。

        旧库（仅有 ``events`` 表）首次升级时在线改名：表 → 列 → 清理旧索引，并把已存的
        ``memory_embeddings.source_kind`` ``'event'`` → ``'note'``（避免向量索引失联重嵌）。
        FTS 行（``kind='event'`` → ``'note'``）的收敛在 ``_migrate_memory_fts`` 内完成。
        对全新库为无操作（无 ``events`` 表）。重复执行安全。
        """
        cursor = conn.cursor()

        def _table_exists(name: str) -> bool:
            cursor.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
                (name,),
            )
            return cursor.fetchone() is not None

        has_old = _table_exists("events")
        has_new = _table_exists("memory_notes")
        if has_old and not has_new:
            cursor.execute("ALTER TABLE events RENAME TO memory_notes")
            has_new = True

        if has_new:
            cursor.execute("PRAGMA table_info(memory_notes)")
            cols = {row[1] for row in cursor.fetchall()}
            if "event_type" in cols and "note_kind" not in cols:
                cursor.execute(
                    "ALTER TABLE memory_notes RENAME COLUMN event_type TO note_kind"
                )
            for old_idx in (
                "idx_events_user",
                "idx_events_type",
                "idx_events_tenant_user",
                "idx_events_dedupe_hash",
            ):
                cursor.execute(f"DROP INDEX IF EXISTS {old_idx}")

        if _table_exists("memory_embeddings"):
            cursor.execute(
                "UPDATE memory_embeddings SET source_kind='note' WHERE source_kind='event'"
            )
        conn.commit()

    def _migrate_note_dedupe_hash(self, conn: sqlite3.Connection) -> None:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(memory_notes)")
        note_cols = {row[1] for row in cursor.fetchall()}
        if "dedupe_hash" not in note_cols:
            cursor.execute("ALTER TABLE memory_notes ADD COLUMN dedupe_hash TEXT")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_notes_dedupe_hash "
            "ON memory_notes(user_id, agent_id, dedupe_hash)"
        )

    def _migrate_memory_fts(self, conn: sqlite3.Connection) -> bool:
        """FTS5：messages + memory_notes；优先 trigram（中文子串），否则 unicode61。"""
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_fts' LIMIT 1"
        )
        if not cursor.fetchone():
            tokenizers = ("'trigram'", "'unicode61'")
            created = False
            for tok in tokenizers:
                try:
                    cursor.execute(
                        f"""
                        CREATE VIRTUAL TABLE memory_fts USING fts5(
                            body,
                            kind UNINDEXED,
                            ref_id UNINDEXED,
                            session_id UNINDEXED,
                            tenant_id UNINDEXED,
                            user_id UNINDEXED,
                            agent_id UNINDEXED,
                            role UNINDEXED,
                            tokenize={tok}
                        )
                        """
                    )
                    created = True
                    break
                except sqlite3.OperationalError:
                    continue
            if not created:
                return False

        cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' "
            "AND name='memory_fts_ai_msg' LIMIT 1"
        )
        if not cursor.fetchone():
            cursor.execute("DROP TRIGGER IF EXISTS memory_fts_ai_msg")
            cursor.execute(
                """
                CREATE TRIGGER memory_fts_ai_msg AFTER INSERT ON messages
                BEGIN
                  INSERT INTO memory_fts(
                    body, kind, ref_id, session_id, tenant_id,
                    user_id, agent_id, role
                  )
                  VALUES (
                    NEW.content, 'message', NEW.id, NEW.session_id,
                    COALESCE(NEW.tenant_id, 'default'),
                    '', '', NEW.role
                  );
                END
                """
            )

        # 记忆要点触发器（memory_notes，kind='note'）。旧库可能遗留 events 触发器
        # （memory_fts_ai_ev）与 kind='event' 的 FTS 行，这里统一收敛到 memory_notes/'note'。
        cursor.execute("DROP TRIGGER IF EXISTS memory_fts_ai_ev")
        cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='memory_fts_ai_note' LIMIT 1"
        )
        note_trig = cursor.fetchone()
        note_trig_sql = (note_trig[0] if note_trig else "") or ""
        note_trig_outdated = bool(note_trig) and "NEW.tenant_id" not in note_trig_sql
        if not note_trig or note_trig_outdated:
            cursor.execute("DROP TRIGGER IF EXISTS memory_fts_ai_note")
            cursor.execute(
                """
                CREATE TRIGGER memory_fts_ai_note AFTER INSERT ON memory_notes
                BEGIN
                  INSERT INTO memory_fts(
                    body, kind, ref_id, session_id, tenant_id,
                    user_id, agent_id, role
                  )
                  VALUES (
                    NEW.content, 'note', NEW.id, '',
                    COALESCE(NEW.tenant_id, 'default'), NEW.user_id, NEW.agent_id, ''
                  );
                END
                """
            )
        # 旧 FTS 行（kind='event'）收敛为 'note'：表改名保留了 id，故从 memory_notes 重建即可。
        cursor.execute("SELECT 1 FROM memory_fts WHERE kind = 'event' LIMIT 1")
        if cursor.fetchone():
            try:
                cursor.execute("DELETE FROM memory_fts WHERE kind IN ('event', 'note')")
                cursor.execute(
                    """
                    INSERT INTO memory_fts(
                        body, kind, ref_id, session_id, tenant_id,
                        user_id, agent_id, role
                    )
                    SELECT
                        content, 'note', id, '',
                        COALESCE(tenant_id, 'default'), user_id, agent_id, ''
                    FROM memory_notes
                    """
                )
            except sqlite3.OperationalError:
                pass

        cursor.execute("SELECT COUNT(*) FROM memory_fts")
        if cursor.fetchone()[0] == 0:
            try:
                cursor.execute(
                    """
                    INSERT INTO memory_fts(
                        body, kind, ref_id, session_id, tenant_id,
                        user_id, agent_id, role
                    )
                    SELECT
                        content, 'message', id, session_id,
                        COALESCE(tenant_id, 'default'),
                        '', '', role
                    FROM messages
                    """
                )
                cursor.execute(
                    """
                    INSERT INTO memory_fts(
                        body, kind, ref_id, session_id, tenant_id,
                        user_id, agent_id, role
                    )
                    SELECT
                        content, 'note', id, '',
                        COALESCE(tenant_id, 'default'), user_id, agent_id, ''
                    FROM memory_notes
                    """
                )
            except sqlite3.OperationalError:
                return False

        return True

    def search_memory_fts(
        self,
        *,
        match_query: str,
        session_id: str,
        tenant_id: str = "default",
        user_id: str = "",
        limit: int = 5,
        raw_query: str = "",
    ) -> list[dict[str, Any]]:
        """
        FTS5 检索（本会话 transcript + 本用户及全局 memory_notes 记忆要点）。
        match_query 须为合法 MATCH 表达式（见 fts5_phrase_query）。
        raw_query 仅供其他后端（如 PostgresStore）使用，SQLite 走 FTS5 故忽略。
        """
        if not match_query:
            return []
        if not self._fts_ready:
            debug(
                "[memory.fts] skip search: fts not ready | "
                f"match={_log_preview(match_query)!r} session={session_id!r}"
            )
            return []

        conn = self._get_connection()
        cursor = conn.cursor()
        used_bm25 = False
        try:
            cursor.execute(
                """
                SELECT body, kind, ref_id, session_id,
                       bm25(memory_fts) AS score
                FROM memory_fts
                WHERE memory_fts MATCH ?
                  AND (
                    (
                      kind = 'message'
                      AND session_id = ?
                      AND tenant_id = ?
                    )
                    OR (
                      kind = 'note'
                      AND tenant_id = ?
                      AND (
                        COALESCE(user_id, '') = ''
                        OR user_id = ?
                      )
                    )
                  )
                ORDER BY score
                LIMIT ?
                """,
                (match_query, session_id, tenant_id, tenant_id, user_id, limit),
            )
            used_bm25 = True
        except sqlite3.OperationalError as e:
            try:
                cursor.execute(
                    """
                    SELECT body, kind, ref_id, session_id
                    FROM memory_fts
                    WHERE memory_fts MATCH ?
                      AND (
                        (
                          kind = 'message'
                          AND session_id = ?
                          AND tenant_id = ?
                        )
                        OR (
                          kind = 'note'
                          AND tenant_id = ?
                          AND (
                            COALESCE(user_id, '') = ''
                            OR user_id = ?
                          )
                        )
                      )
                    LIMIT ?
                    """,
                    (match_query, session_id, tenant_id, tenant_id, user_id, limit),
                )
            except sqlite3.OperationalError as e2:
                debug(
                    "[memory.fts] MATCH failed | "
                    f"match={_log_preview(match_query)!r} session={session_id!r} "
                    f"err={e2!r} (bm25_err={e!r})"
                )
                return []
        rows = cursor.fetchall()
        out = [dict(row) for row in rows]
        kinds = ",".join(sorted({str(r.get("kind") or "") for r in out})) or "-"
        debug(
            "[memory.fts] search | "
            f"hits={len(out)} kinds={kinds} bm25={used_bm25} "
            f"match={_log_preview(match_query)!r} session={session_id!r} "
            f"tenant={tenant_id!r} user={user_id!r} limit={limit}"
        )
        return out

    @staticmethod
    def content_hash(content: str) -> str:
        return hashlib.sha256((content or "").encode("utf-8")).hexdigest()

    def get_memory_source_records(
        self,
        *,
        session_id: str,
        tenant_id: str = "default",
        user_id: str = "",
        agent_id: str = "",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """取可用于 embedding 索引的消息、摘要和事件。"""
        conn = self._get_connection()
        cursor = conn.cursor()
        records: list[dict[str, Any]] = []
        per_bucket = max(10, limit // 3)

        cursor.execute(
            "SELECT id, content, role, created_at FROM messages "
            "WHERE session_id = ? AND tenant_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (session_id, tenant_id, per_bucket),
        )
        for row in cursor.fetchall():
            records.append({
                "source_kind": "message",
                "source_id": str(row["id"]),
                "body": row["content"],
                "tenant_id": tenant_id,
                "user_id": user_id,
                "agent_id": agent_id,
                "session_id": session_id,
                "role": row["role"],
                "citation": f"message#{row['id']}",
                "created_at": row["created_at"],
            })

        cursor.execute(
            "SELECT id, summary, created_at FROM summaries "
            "WHERE session_id = ? AND tenant_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (session_id, tenant_id, max(3, per_bucket // 4)),
        )
        for row in cursor.fetchall():
            records.append({
                "source_kind": "summary",
                "source_id": str(row["id"]),
                "body": row["summary"],
                "tenant_id": tenant_id,
                "user_id": user_id,
                "agent_id": agent_id,
                "session_id": session_id,
                "role": "summary",
                "citation": f"summary#{row['id']}",
                "created_at": row["created_at"],
            })

        cursor.execute(
            "SELECT id, content, note_kind, importance, created_at FROM memory_notes "
            "WHERE tenant_id = ? "
            "AND (COALESCE(user_id, '') = '' OR user_id = ?) "
            "AND (COALESCE(agent_id, '') = '' OR agent_id = ?) "
            "ORDER BY importance DESC, created_at DESC, id DESC LIMIT ?",
            (tenant_id, user_id, agent_id, per_bucket),
        )
        for row in cursor.fetchall():
            records.append({
                "source_kind": "note",
                "source_id": str(row["id"]),
                "body": row["content"],
                "tenant_id": tenant_id,
                "user_id": user_id,
                "agent_id": agent_id,
                "session_id": "",
                "role": row["note_kind"],
                "citation": f"note#{row['id']}",
                "created_at": row["created_at"],
            })

        return records[:limit]

    def get_memory_record(
        self,
        *,
        source_kind: str,
        source_id: str,
        tenant_id: str = "default",
        user_id: str = "",
        agent_id: str = "",
    ) -> Optional[dict[str, Any]]:
        conn = self._get_connection()
        cursor = conn.cursor()
        if source_kind == "message":
            cursor.execute(
                "SELECT id, content, role, session_id, created_at FROM messages "
                "WHERE id = ? AND tenant_id = ?",
                (source_id, tenant_id),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return {
                "source_kind": "message",
                "source_id": str(row["id"]),
                "body": row["content"],
                "role": row["role"],
                "session_id": row["session_id"],
                "citation": f"message#{row['id']}",
                "created_at": row["created_at"],
            }
        if source_kind == "summary":
            cursor.execute(
                "SELECT id, summary, session_id, created_at FROM summaries "
                "WHERE id = ? AND tenant_id = ?",
                (source_id, tenant_id),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return {
                "source_kind": "summary",
                "source_id": str(row["id"]),
                "body": row["summary"],
                "role": "summary",
                "session_id": row["session_id"],
                "citation": f"summary#{row['id']}",
                "created_at": row["created_at"],
            }
        if source_kind == "note":
            cursor.execute(
                "SELECT id, content, note_kind, importance, created_at FROM memory_notes "
                "WHERE id = ? AND tenant_id = ? "
                "AND (COALESCE(user_id, '') = '' OR user_id = ?) "
                "AND (COALESCE(agent_id, '') = '' OR agent_id = ?)",
                (source_id, tenant_id, user_id, agent_id),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return {
                "source_kind": "note",
                "source_id": str(row["id"]),
                "body": row["content"],
                "role": row["note_kind"],
                "importance": row["importance"],
                "session_id": "",
                "citation": f"note#{row['id']}",
                "created_at": row["created_at"],
            }
        return None

    def get_embedding(
        self,
        *,
        source_kind: str,
        source_id: str,
        embedding_model: str,
    ) -> Optional[dict[str, Any]]:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM memory_embeddings "
            "WHERE source_kind = ? AND source_id = ? AND embedding_model = ?",
            (source_kind, source_id, embedding_model),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def upsert_embedding(
        self,
        *,
        source_kind: str,
        source_id: str,
        tenant_id: str,
        user_id: str,
        agent_id: str,
        content: str,
        embedding_model: str,
        vector: list[float],
    ) -> None:
        conn = self._get_connection()
        conn.execute(
            """
            INSERT INTO memory_embeddings (
                source_kind, source_id, tenant_id, user_id, agent_id,
                content_hash, embedding_model, dimensions, embedding_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_kind, source_id, embedding_model)
            DO UPDATE SET
                tenant_id = excluded.tenant_id,
                user_id = excluded.user_id,
                agent_id = excluded.agent_id,
                content_hash = excluded.content_hash,
                dimensions = excluded.dimensions,
                embedding_json = excluded.embedding_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                source_kind,
                source_id,
                tenant_id,
                user_id,
                agent_id,
                self.content_hash(content),
                embedding_model,
                len(vector),
                json.dumps(vector, separators=(",", ":")),
            ),
        )
        conn.commit()

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tokens: int = 0,
        tool_name: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        tenant_id: str = "default",
    ) -> int:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO messages
            (session_id, role, content, tokens, tool_name, tool_call_id, tenant_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, role, content, tokens, tool_name, tool_call_id, tenant_id),
        )
        conn.commit()
        return cursor.lastrowid or 0

    def get_messages(
        self,
        session_id: str,
        limit: int = 100,
        offset: int = 0,
        tenant_id: str = "default",
    ) -> list[dict[str, Any]]:
        """
        返回本会话「最近」的若干条消息，按时间正序（适合作 LLM 上下文与滚动摘要）。

        先按 created_at DESC, id DESC 取 limit 条（offset 跳过最新的 offset 条，用于翻页取更早片段），
        再反转为时间升序。
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM messages WHERE session_id = ? AND tenant_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (session_id, tenant_id, limit, offset),
        )
        rows = [dict(row) for row in cursor.fetchall()]
        rows.reverse()
        return rows

    def get_message_count(self, session_id: str, tenant_id: str = "default") -> int:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ? AND tenant_id = ?",
            (session_id, tenant_id),
        )
        return cursor.fetchone()[0]

    def add_summary(
        self,
        session_id: str,
        summary: str,
        original_count: int,
        summary_type: str = "auto",
        tenant_id: str = "default",
    ) -> int:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO summaries "
            "(session_id, summary, original_count, summary_type, tenant_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, summary, original_count, summary_type, tenant_id),
        )
        conn.commit()
        return cursor.lastrowid or 0

    def get_latest_summary(
        self,
        session_id: str,
        tenant_id: str = "default",
    ) -> Optional[dict[str, Any]]:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM summaries WHERE session_id = ? AND tenant_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (session_id, tenant_id),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_summaries(
        self,
        session_id: str,
        limit: int = 10,
        tenant_id: str = "default",
    ) -> list[dict[str, Any]]:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM summaries WHERE session_id = ? AND tenant_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (session_id, tenant_id, limit),
        )
        return [dict(row) for row in cursor.fetchall()]

    def note_exists_by_dedupe_hash(
        self,
        dedupe_hash: str,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        tenant_id: str = "default",
    ) -> bool:
        """是否已有相同规范化键的记忆要点（自动抽取去重，按租户隔离）。"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM memory_notes WHERE tenant_id = ? "
            "AND COALESCE(user_id, '') = COALESCE(?, '') "
            "AND COALESCE(agent_id, '') = COALESCE(?, '') "
            "AND dedupe_hash = ? LIMIT 1",
            (tenant_id, user_id, agent_id, dedupe_hash),
        )
        return cursor.fetchone() is not None

    def add_memory_note(
        self,
        note_kind: str,
        content: str,
        importance: int = 5,
        tags: Optional[str] = None,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        *,
        dedupe: bool = False,
        tenant_id: str = "default",
    ) -> int:
        conn = self._get_connection()
        cursor = conn.cursor()
        h = note_row_hash(
            note_kind=note_kind,
            content=content,
            user_id=user_id,
            agent_id=agent_id,
        )
        if dedupe and self.note_exists_by_dedupe_hash(
            h, user_id=user_id, agent_id=agent_id, tenant_id=tenant_id
        ):
            return 0
        cursor.execute(
            "INSERT INTO memory_notes "
            "(note_kind, content, importance, tags, user_id, agent_id, tenant_id, dedupe_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (note_kind, content, importance, tags, user_id, agent_id, tenant_id, h),
        )
        conn.commit()
        return cursor.lastrowid or 0

    def get_memory_notes(
        self,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        note_kind: Optional[str] = None,
        limit: int = 50,
        tenant_id: str = "default",
    ) -> list[dict[str, Any]]:
        conn = self._get_connection()
        cursor = conn.cursor()

        # tenant_id 强制过滤（数据库层纵深防御）。
        query = "SELECT * FROM memory_notes WHERE tenant_id = ?"
        params: list[Any] = [tenant_id]

        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)
        if agent_id:
            query += " AND agent_id = ?"
            params.append(agent_id)
        if note_kind:
            query += " AND note_kind = ?"
            params.append(note_kind)

        query += " ORDER BY importance DESC, created_at DESC LIMIT ?"
        params.append(limit)

        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def set_profile(
        self,
        user_id: str,
        agent_id: str,
        key: str,
        value: str,
        confidence: int = 5,
        tenant_id: str = "default",
    ) -> None:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO user_profile (user_id, agent_id, tenant_id, key, value, confidence)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, user_id, agent_id, key)
            DO UPDATE SET value = excluded.value,
                          confidence = excluded.confidence,
                          tenant_id = excluded.tenant_id,
                          updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, agent_id, tenant_id, key, value, confidence),
        )
        conn.commit()

    def get_profile(
        self, user_id: str, agent_id: str, tenant_id: str = "default"
    ) -> dict[str, str]:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT key, value FROM user_profile "
            "WHERE tenant_id = ? AND user_id = ? AND agent_id = ?",
            (tenant_id, user_id, agent_id),
        )
        return {row["key"]: row["value"] for row in cursor.fetchall()}

    def vacuum_if_needed(self) -> None:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT page_count * page_size as size "
            "FROM pragma_page_count(), pragma_page_size()"
        )
        size_mb = cursor.fetchone()[0] / (1024 * 1024)
        if size_mb > self.max_size_mb:
            cursor.execute("VACUUM")
            conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
