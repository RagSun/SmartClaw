"""PostgreSQL 记忆存储层（共享库，多实例一致）。见 progress.md §12.2。

设计要点：
- **公共方法签名与 SQLiteStore 完全一致**，由 ``create_memory_store`` 工厂透明替换。
- 单机 SQLite 靠「一 agent 一文件」做物理隔离；合并到共享库后用 **(tenant_id, agent_id)
  应用层强制过滤**（每条 SQL 都带），等价并叠加纵深防御。``PostgresStore`` 在构造时
  绑定 ``agent_id``（如同 SQLite 的 db 文件名编码了 agent）。
- 全文检索用 **pg_trgm**（GIN 索引）做中文子串匹配，``similarity()`` 排序。
- **会话级 advisory 锁**（``session_lock``）：``pg_advisory_lock``，防多实例交叉写同一对话。
- 连接走 **连接池**（``psycopg_pool``），按 DSN 进程内复用（多个 agent 的 store 共享池）。

行级安全（RLS）说明：
- 本后端默认**不开 RLS**，因为：①应用层每条查询已强制 ``tenant_id`` 过滤；②PG 超级用户
  （docker 默认 ``postgres``）始终绕过 RLS，会让「开了等于没开」难以验证 → 半成品风险。
- 需要 DB 层物理隔离者，按 ``scripts/migrate_sqlite_to_postgres.py`` 文末「可选 RLS 硬化」用
  **非超级用户角色** + ``FORCE ROW LEVEL SECURITY`` 启用（含 ``SET app.tenant_id`` 用法）。
"""

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from smartclaw.console import debug
from smartclaw.memory.hash_dedupe import note_row_hash

# 进程内按 DSN 复用连接池 + 记录已初始化过的 DSN（避免每个 agent 重复建表）。
_POOLS: dict[str, ConnectionPool] = {}
_INITIALIZED: set[str] = set()
_LOCK = threading.Lock()


def _get_pool(dsn: str) -> ConnectionPool:
    with _LOCK:
        pool = _POOLS.get(dsn)
        if pool is None:
            # autocommit：每条语句独立提交，单方法多为单语句；跨语句原子性由调用方
            # （runner 回合 / session_lock）负责。
            pool = ConnectionPool(
                conninfo=dsn,
                min_size=1,
                max_size=10,
                kwargs={"autocommit": True},
                open=True,
                name="smartclaw-memory",
            )
            _POOLS[dsn] = pool
        return pool


class PostgresStore:
    """记忆数据面 PostgreSQL 后端。构造即绑定 ``agent_id``（等价 SQLite 的分文件）。"""

    def __init__(self, dsn: str, agent_id: str = "default") -> None:
        self.dsn = dsn
        self.agent_id = agent_id or "default"
        self._pool = _get_pool(dsn)
        self._fts_ready: bool = True  # pg_trgm 启用后即可检索

    # ------------------------------------------------------------------ #
    # 连接 / 初始化
    # ------------------------------------------------------------------ #
    @contextmanager
    def _conn(self) -> Iterator[psycopg.Connection]:
        with self._pool.connection() as conn:
            yield conn

    def initialize(self) -> None:
        if self.dsn in _INITIALIZED:
            return
        with _LOCK:
            if self.dsn in _INITIALIZED:
                return
            with self._pool.connection() as conn, conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
                cur.execute(_DDL_MESSAGES)
                cur.execute(_DDL_SUMMARIES)
                self._migrate_events_to_memory_notes(cur)
                cur.execute(_DDL_MEMORY_NOTES)
                cur.execute(_DDL_USER_PROFILE)
                cur.execute(_DDL_EMBEDDINGS)
                for stmt in _DDL_INDEXES:
                    cur.execute(stmt)
            _INITIALIZED.add(self.dsn)

    @staticmethod
    def _migrate_events_to_memory_notes(cur: Any) -> None:
        """技术债清除：共享库的 ``events`` → ``memory_notes``、``event_type`` → ``note_kind`` 幂等迁移。

        旧共享库首次升级时在线改名（表 → 列 → 旧索引清理），并把已存的
        ``memory_embeddings.source_kind`` ``'event'`` → ``'note'``。全新库为无操作。
        """

        def _table_exists(name: str) -> bool:
            cur.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name=%s LIMIT 1",
                (name,),
            )
            return cur.fetchone() is not None

        def _column_exists(table: str, column: str) -> bool:
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=%s AND column_name=%s LIMIT 1",
                (table, column),
            )
            return cur.fetchone() is not None

        has_old = _table_exists("events")
        has_new = _table_exists("memory_notes")
        if has_old and not has_new:
            cur.execute("ALTER TABLE events RENAME TO memory_notes")
            has_new = True
        if has_new and _column_exists("memory_notes", "event_type") and not _column_exists(
            "memory_notes", "note_kind"
        ):
            cur.execute("ALTER TABLE memory_notes RENAME COLUMN event_type TO note_kind")
        for old_idx in (
            "idx_pg_events_scope",
            "idx_pg_events_dedupe",
            "idx_pg_events_trgm",
        ):
            cur.execute(f"DROP INDEX IF EXISTS {old_idx}")
        if _table_exists("memory_embeddings"):
            cur.execute(
                "UPDATE memory_embeddings SET source_kind='note' WHERE source_kind='event'"
            )

    @staticmethod
    def content_hash(content: str) -> str:
        return hashlib.sha256((content or "").encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------ #
    # 会话级分布式锁（多实例串行化同一对话的读改写）
    # ------------------------------------------------------------------ #
    @contextmanager
    def session_lock(self, tenant_id: str, session_id: str) -> Iterator[None]:
        """阻塞式获取 ``(tenant_id, session_id)`` 的 advisory 锁，退出释放。

        不同会话并行、同一会话串行；跨实例由 PostgreSQL 统一仲裁。
        """
        key = f"{tenant_id}:{session_id}"
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (key,))
            try:
                yield
            finally:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,)
                    )

    # ------------------------------------------------------------------ #
    # messages
    # ------------------------------------------------------------------ #
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
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO messages "
                "(tenant_id, agent_id, session_id, role, content, tokens, tool_name, tool_call_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (tenant_id, self.agent_id, session_id, role, content, tokens,
                 tool_name, tool_call_id),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def get_messages(
        self,
        session_id: str,
        limit: int = 100,
        offset: int = 0,
        tenant_id: str = "default",
    ) -> list[dict[str, Any]]:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, session_id, role, content, tokens, tool_name, tool_call_id, "
                "tenant_id, agent_id, created_at::text AS created_at "
                "FROM messages WHERE session_id=%s AND tenant_id=%s AND agent_id=%s "
                "ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
                (session_id, tenant_id, self.agent_id, limit, offset),
            )
            rows = cur.fetchall()
        rows.reverse()
        return rows

    def get_message_count(self, session_id: str, tenant_id: str = "default") -> int:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM messages "
                "WHERE session_id=%s AND tenant_id=%s AND agent_id=%s",
                (session_id, tenant_id, self.agent_id),
            )
            return int(cur.fetchone()[0])

    # ------------------------------------------------------------------ #
    # summaries
    # ------------------------------------------------------------------ #
    def add_summary(
        self,
        session_id: str,
        summary: str,
        original_count: int,
        summary_type: str = "auto",
        tenant_id: str = "default",
    ) -> int:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO summaries "
                "(tenant_id, agent_id, session_id, summary, original_count, summary_type) "
                "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                (tenant_id, self.agent_id, session_id, summary, original_count, summary_type),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def get_latest_summary(
        self, session_id: str, tenant_id: str = "default"
    ) -> Optional[dict[str, Any]]:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, session_id, summary, original_count, summary_type, "
                "tenant_id, agent_id, created_at::text AS created_at "
                "FROM summaries WHERE session_id=%s AND tenant_id=%s AND agent_id=%s "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (session_id, tenant_id, self.agent_id),
            )
            return cur.fetchone()

    def get_summaries(
        self, session_id: str, limit: int = 10, tenant_id: str = "default"
    ) -> list[dict[str, Any]]:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, session_id, summary, original_count, summary_type, "
                "tenant_id, agent_id, created_at::text AS created_at "
                "FROM summaries WHERE session_id=%s AND tenant_id=%s AND agent_id=%s "
                "ORDER BY created_at DESC, id DESC LIMIT %s",
                (session_id, tenant_id, self.agent_id, limit),
            )
            return cur.fetchall()

    # ------------------------------------------------------------------ #
    # memory_notes（记忆要点）
    # ------------------------------------------------------------------ #
    def note_exists_by_dedupe_hash(
        self,
        dedupe_hash: str,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        tenant_id: str = "default",
    ) -> bool:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM memory_notes WHERE tenant_id=%s "
                "AND COALESCE(user_id,'')=COALESCE(%s,'') "
                "AND COALESCE(agent_id,'')=COALESCE(%s,'') "
                "AND dedupe_hash=%s LIMIT 1",
                (tenant_id, user_id, agent_id, dedupe_hash),
            )
            return cur.fetchone() is not None

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
        h = note_row_hash(
            note_kind=note_kind, content=content, user_id=user_id, agent_id=agent_id
        )
        if dedupe and self.note_exists_by_dedupe_hash(
            h, user_id=user_id, agent_id=agent_id, tenant_id=tenant_id
        ):
            return 0
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO memory_notes "
                "(tenant_id, agent_id, user_id, note_kind, content, importance, tags, dedupe_hash) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (tenant_id, agent_id, user_id, note_kind, content, importance, tags, h),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def get_memory_notes(
        self,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        note_kind: Optional[str] = None,
        limit: int = 50,
        tenant_id: str = "default",
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT id, tenant_id, agent_id, user_id, note_kind, content, importance, "
            "tags, dedupe_hash, created_at::text AS created_at FROM memory_notes WHERE tenant_id=%s"
        )
        params: list[Any] = [tenant_id]
        if user_id:
            query += " AND user_id=%s"
            params.append(user_id)
        if agent_id:
            query += " AND agent_id=%s"
            params.append(agent_id)
        if note_kind:
            query += " AND note_kind=%s"
            params.append(note_kind)
        query += " ORDER BY importance DESC, created_at DESC LIMIT %s"
        params.append(limit)
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            return cur.fetchall()

    # ------------------------------------------------------------------ #
    # user_profile
    # ------------------------------------------------------------------ #
    def set_profile(
        self,
        user_id: str,
        agent_id: str,
        key: str,
        value: str,
        confidence: int = 5,
        tenant_id: str = "default",
    ) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_profile "
                "(tenant_id, user_id, agent_id, key, value, confidence) "
                "VALUES (%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (tenant_id, user_id, agent_id, key) DO UPDATE SET "
                "value=EXCLUDED.value, confidence=EXCLUDED.confidence, "
                "updated_at=now()",
                (tenant_id, user_id, agent_id, key, value, confidence),
            )

    def get_profile(
        self, user_id: str, agent_id: str, tenant_id: str = "default"
    ) -> dict[str, str]:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT key, value FROM user_profile "
                "WHERE tenant_id=%s AND user_id=%s AND agent_id=%s",
                (tenant_id, user_id, agent_id),
            )
            return {row[0]: row[1] for row in cur.fetchall()}

    # ------------------------------------------------------------------ #
    # embeddings
    # ------------------------------------------------------------------ #
    def get_embedding(
        self, *, source_kind: str, source_id: str, embedding_model: str
    ) -> Optional[dict[str, Any]]:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM memory_embeddings "
                "WHERE source_kind=%s AND source_id=%s AND embedding_model=%s",
                (source_kind, source_id, embedding_model),
            )
            return cur.fetchone()

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
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO memory_embeddings "
                "(source_kind, source_id, tenant_id, user_id, agent_id, content_hash, "
                "embedding_model, dimensions, embedding_json) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (source_kind, source_id, embedding_model) DO UPDATE SET "
                "tenant_id=EXCLUDED.tenant_id, user_id=EXCLUDED.user_id, "
                "agent_id=EXCLUDED.agent_id, content_hash=EXCLUDED.content_hash, "
                "dimensions=EXCLUDED.dimensions, embedding_json=EXCLUDED.embedding_json, "
                "updated_at=now()",
                (source_kind, source_id, tenant_id, user_id, agent_id,
                 self.content_hash(content), embedding_model, len(vector),
                 json.dumps(vector, separators=(",", ":"))),
            )

    # ------------------------------------------------------------------ #
    # 检索源记录 / 单条取回（embedding 索引用）
    # ------------------------------------------------------------------ #
    def get_memory_source_records(
        self,
        *,
        session_id: str,
        tenant_id: str = "default",
        user_id: str = "",
        agent_id: str = "",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        aid = agent_id or self.agent_id
        records: list[dict[str, Any]] = []
        per_bucket = max(10, limit // 3)
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, content, role, created_at::text AS created_at FROM messages "
                "WHERE session_id=%s AND tenant_id=%s AND agent_id=%s "
                "ORDER BY created_at DESC, id DESC LIMIT %s",
                (session_id, tenant_id, self.agent_id, per_bucket),
            )
            for row in cur.fetchall():
                records.append({
                    "source_kind": "message", "source_id": str(row["id"]),
                    "body": row["content"], "tenant_id": tenant_id, "user_id": user_id,
                    "agent_id": aid, "session_id": session_id, "role": row["role"],
                    "citation": f"message#{row['id']}", "created_at": row["created_at"],
                })
            cur.execute(
                "SELECT id, summary, created_at::text AS created_at FROM summaries "
                "WHERE session_id=%s AND tenant_id=%s AND agent_id=%s "
                "ORDER BY created_at DESC, id DESC LIMIT %s",
                (session_id, tenant_id, self.agent_id, max(3, per_bucket // 4)),
            )
            for row in cur.fetchall():
                records.append({
                    "source_kind": "summary", "source_id": str(row["id"]),
                    "body": row["summary"], "tenant_id": tenant_id, "user_id": user_id,
                    "agent_id": aid, "session_id": session_id, "role": "summary",
                    "citation": f"summary#{row['id']}", "created_at": row["created_at"],
                })
            cur.execute(
                "SELECT id, content, note_kind, importance, created_at::text AS created_at "
                "FROM memory_notes WHERE tenant_id=%s "
                "AND (COALESCE(user_id,'')='' OR user_id=%s) "
                "AND (COALESCE(agent_id,'')='' OR agent_id=%s) "
                "ORDER BY importance DESC, created_at DESC, id DESC LIMIT %s",
                (tenant_id, user_id, aid, per_bucket),
            )
            for row in cur.fetchall():
                records.append({
                    "source_kind": "note", "source_id": str(row["id"]),
                    "body": row["content"], "tenant_id": tenant_id, "user_id": user_id,
                    "agent_id": aid, "session_id": "", "role": row["note_kind"],
                    "citation": f"note#{row['id']}", "created_at": row["created_at"],
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
        if not str(source_id).isdigit():
            return None
        sid = int(source_id)
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            if source_kind == "message":
                cur.execute(
                    "SELECT id, content, role, session_id, created_at::text AS created_at "
                    "FROM messages WHERE id=%s AND tenant_id=%s AND agent_id=%s",
                    (sid, tenant_id, self.agent_id),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "source_kind": "message", "source_id": str(row["id"]),
                    "body": row["content"], "role": row["role"],
                    "session_id": row["session_id"],
                    "citation": f"message#{row['id']}", "created_at": row["created_at"],
                }
            if source_kind == "summary":
                cur.execute(
                    "SELECT id, summary, session_id, created_at::text AS created_at "
                    "FROM summaries WHERE id=%s AND tenant_id=%s AND agent_id=%s",
                    (sid, tenant_id, self.agent_id),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "source_kind": "summary", "source_id": str(row["id"]),
                    "body": row["summary"], "role": "summary",
                    "session_id": row["session_id"],
                    "citation": f"summary#{row['id']}", "created_at": row["created_at"],
                }
            if source_kind == "note":
                cur.execute(
                    "SELECT id, content, note_kind, importance, created_at::text AS created_at "
                    "FROM memory_notes WHERE id=%s AND tenant_id=%s "
                    "AND (COALESCE(user_id,'')='' OR user_id=%s) "
                    "AND (COALESCE(agent_id,'')='' OR agent_id=%s)",
                    (sid, tenant_id, user_id, agent_id or self.agent_id),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "source_kind": "note", "source_id": str(row["id"]),
                    "body": row["content"], "role": row["note_kind"],
                    "importance": row["importance"], "session_id": "",
                    "citation": f"note#{row['id']}", "created_at": row["created_at"],
                }
        return None

    # ------------------------------------------------------------------ #
    # 全文检索（pg_trgm 中文子串）
    # ------------------------------------------------------------------ #
    def search_memory_fts(
        self,
        *,
        match_query: str = "",
        session_id: str,
        tenant_id: str = "default",
        user_id: str = "",
        limit: int = 5,
        raw_query: str = "",
    ) -> list[dict[str, Any]]:
        """pg_trgm 子串检索：本会话 messages + 本用户/全局 memory_notes（记忆要点），按相似度排序。

        与 SQLite FTS5 行为对齐的过滤：message 限本 session+tenant+agent；memory_note 限
        tenant+agent，且 user_id 为空(全局)或匹配本用户。``raw_query`` 为用户原句
        （manager 传入）；为空时从 ``match_query`` 去引号兜底。
        """
        q = (raw_query or match_query or "").strip().strip('"').replace('""', '"')
        if not q:
            return []
        like = f"%{q}%"
        sql = (
            "SELECT body, kind, ref_id, session_id, score FROM ("
            "  SELECT content AS body, 'message' AS kind, id AS ref_id, session_id, "
            "         similarity(content, %(q)s) AS score "
            "  FROM messages "
            "  WHERE session_id=%(sid)s AND tenant_id=%(tid)s AND agent_id=%(aid)s "
            "        AND content ILIKE %(like)s "
            "  UNION ALL "
            "  SELECT content AS body, 'note' AS kind, id AS ref_id, ''::text AS session_id, "
            "         similarity(content, %(q)s) AS score "
            "  FROM memory_notes "
            "  WHERE tenant_id=%(tid)s "
            "        AND (COALESCE(user_id,'')='' OR user_id=%(uid)s) "
            "        AND (COALESCE(agent_id,'')='' OR agent_id=%(aid)s) "
            "        AND content ILIKE %(like)s "
            ") hits ORDER BY score DESC, ref_id DESC LIMIT %(limit)s"
        )
        params = {
            "q": q, "like": like, "sid": session_id, "tid": tenant_id,
            "aid": self.agent_id, "uid": user_id, "limit": limit,
        }
        try:
            with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                out = cur.fetchall()
        except psycopg.Error as e:
            debug(f"[memory.pg.fts] search failed: {e}")
            return []
        debug(
            f"[memory.pg.fts] hits={len(out)} q={q[:48]!r} "
            f"session={session_id!r} tenant={tenant_id!r}"
        )
        return out

    # ------------------------------------------------------------------ #
    # 杂项
    # ------------------------------------------------------------------ #
    def vacuum_if_needed(self) -> None:
        # PostgreSQL 由 autovacuum 负责，无需手工触发。
        return None

    def close(self) -> None:
        # 连接池按 DSN 进程级共享，单个 store 关闭不关池（其他 agent 仍在用）。
        return None


# --------------------------------------------------------------------------- #
# DDL
# --------------------------------------------------------------------------- #
_DDL_MESSAGES = """
CREATE TABLE IF NOT EXISTS messages (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    agent_id  TEXT NOT NULL DEFAULT 'default',
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tokens INT DEFAULT 0,
    tool_name TEXT,
    tool_call_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_DDL_SUMMARIES = """
CREATE TABLE IF NOT EXISTS summaries (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    agent_id  TEXT NOT NULL DEFAULT 'default',
    session_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    original_count INT DEFAULT 0,
    summary_type TEXT DEFAULT 'auto',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_DDL_MEMORY_NOTES = """
CREATE TABLE IF NOT EXISTS memory_notes (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    agent_id TEXT,
    user_id TEXT,
    note_kind TEXT NOT NULL,
    content TEXT NOT NULL,
    importance INT DEFAULT 5,
    tags TEXT,
    dedupe_hash TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_DDL_USER_PROFILE = """
CREATE TABLE IF NOT EXISTS user_profile (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    user_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    confidence INT DEFAULT 5,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_pg_user_profile UNIQUE (tenant_id, user_id, agent_id, key)
)
"""

_DDL_EMBEDDINGS = """
CREATE TABLE IF NOT EXISTS memory_embeddings (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    user_id TEXT,
    agent_id TEXT,
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    dimensions INT DEFAULT 0,
    embedding_json TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_pg_memory_embeddings UNIQUE (source_kind, source_id, embedding_model)
)
"""

_DDL_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_pg_messages_scope "
    "ON messages(tenant_id, agent_id, session_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_pg_messages_trgm "
    "ON messages USING gin (content gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS idx_pg_summaries_scope "
    "ON summaries(tenant_id, agent_id, session_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_pg_memory_notes_scope "
    "ON memory_notes(tenant_id, user_id, agent_id)",
    "CREATE INDEX IF NOT EXISTS idx_pg_memory_notes_dedupe "
    "ON memory_notes(tenant_id, agent_id, user_id, dedupe_hash)",
    "CREATE INDEX IF NOT EXISTS idx_pg_memory_notes_trgm "
    "ON memory_notes USING gin (content gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS idx_pg_embeddings_scope "
    "ON memory_embeddings(tenant_id, user_id, agent_id, embedding_model)",
]
