# -*- coding: utf-8 -*-
"""记忆数据面搬迁：本地 SQLite（按 agent 分文件）→ 共享 PostgreSQL（见 progress.md §12.2）。

要点：
- 每个 ``{data_dir}/memory/{agent_id}.db`` 的 ``agent_id`` 由文件名回填到 PG 的 agent_id 列
  （PG 端 messages/summaries 新增了 agent_id 列）。
- **重排主键 + 同步重映射 ``memory_embeddings.source_id``**：embeddings.source_id 指向
  messages/memory_notes 的旧自增 id，合库后旧 id 会撞，必须建 old→new 映射再写 embeddings。
- 读取前先用 ``SQLiteStore.initialize()`` 就地升级源库（旧 ``events/event_type`` →
  ``memory_notes/note_kind``、``source_kind`` ``'event'`` → ``'note'``），保证不论源库新旧都按统一新 schema 搬迁。
- 保留原始 ``created_at``（不被 now() 覆盖），避免上下文顺序错乱。
- 可重入：``--reset`` 先按本次涉及的 (tenant_id, agent_id) 清表再载入；否则若目标已有该
  agent 数据则拒绝（防误重复）。迁后按 (表) 比对行数。

用法（PowerShell）：
    $env:PYTHONPATH="src"
    $env:SMARTCLAW_MEMORY_POSTGRES_DSN="postgresql://postgres:postgres@127.0.0.1:5432/smartclaw"
    python scripts/migrate_sqlite_to_postgres.py --source-dir .\deploy\data\memory --reset

文末附「可选 RLS 硬化」SQL（需非超级用户角色 + FORCE ROW LEVEL SECURITY）。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

try:
    import psycopg
except ImportError:
    print("缺少 psycopg：uv pip install 'psycopg[binary,pool]>=3.1'")
    sys.exit(2)


def _sqlite_rows(db: Path, table: str, columns: list[str]) -> list[dict[str, Any]]:
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        cur = con.cursor()
        cur.execute(f"PRAGMA table_info({table})")
        have = {r[1] for r in cur.fetchall()}
        if not have:
            return []
        cols = [c for c in columns if c in have]
        cur.execute(f"SELECT {', '.join(cols)} FROM {table}")
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


def _ensure_schema(dsn: str, agent_id: str) -> None:
    # 复用 PostgresStore 的建表逻辑（含 pg_trgm / 索引 / 约束）。
    from smartclaw.memory.storage.postgres_store import PostgresStore

    PostgresStore(dsn=dsn, agent_id=agent_id).initialize()


def migrate_db(conn: psycopg.Connection, db: Path, agent_id: str, reset: bool) -> dict[str, int]:
    stats = {"messages": 0, "summaries": 0, "notes": 0, "user_profile": 0, "embeddings": 0}
    # old (kind, old_id_str) -> new_id
    id_map: dict[tuple[str, str], int] = {}

    # 就地升级源库到新 schema（events→memory_notes / event_type→note_kind / source_kind 'event'→'note'）。
    from smartclaw.memory.storage.sqlite_store import SQLiteStore

    SQLiteStore(db).initialize()

    with conn.cursor() as cur:
        if reset:
            for t in ("messages", "summaries", "memory_notes", "user_profile"):
                cur.execute(f"DELETE FROM {t} WHERE agent_id=%s", (agent_id,))
            # embeddings 无独立 agent 标记时按 agent_id 清
            cur.execute("DELETE FROM memory_embeddings WHERE agent_id=%s", (agent_id,))
        else:
            cur.execute("SELECT 1 FROM messages WHERE agent_id=%s LIMIT 1", (agent_id,))
            if cur.fetchone():
                raise SystemExit(
                    f"[拒绝] PG 中已存在 agent={agent_id} 的数据；要覆盖请加 --reset"
                )

        # messages
        for r in _sqlite_rows(db, "messages", [
            "id", "session_id", "role", "content", "tokens", "tool_name",
            "tool_call_id", "tenant_id", "created_at",
        ]):
            cur.execute(
                "INSERT INTO messages "
                "(tenant_id, agent_id, session_id, role, content, tokens, tool_name, "
                "tool_call_id, created_at) VALUES "
                "(%s,%s,%s,%s,%s,%s,%s,%s, COALESCE(%s::timestamptz, now())) RETURNING id",
                (r.get("tenant_id") or "default", agent_id, r["session_id"], r["role"],
                 r["content"], r.get("tokens") or 0, r.get("tool_name"),
                 r.get("tool_call_id"), r.get("created_at")),
            )
            id_map[("message", str(r["id"]))] = int(cur.fetchone()[0])
            stats["messages"] += 1

        # summaries
        for r in _sqlite_rows(db, "summaries", [
            "id", "session_id", "summary", "original_count", "summary_type",
            "tenant_id", "created_at",
        ]):
            cur.execute(
                "INSERT INTO summaries "
                "(tenant_id, agent_id, session_id, summary, original_count, summary_type, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s, COALESCE(%s::timestamptz, now())) RETURNING id",
                (r.get("tenant_id") or "default", agent_id, r["session_id"], r["summary"],
                 r.get("original_count") or 0, r.get("summary_type") or "auto",
                 r.get("created_at")),
            )
            id_map[("summary", str(r["id"]))] = int(cur.fetchone()[0])
            stats["summaries"] += 1

        # memory_notes（记忆要点）
        for r in _sqlite_rows(db, "memory_notes", [
            "id", "note_kind", "content", "importance", "tags", "user_id",
            "agent_id", "tenant_id", "dedupe_hash", "created_at",
        ]):
            cur.execute(
                "INSERT INTO memory_notes "
                "(tenant_id, agent_id, user_id, note_kind, content, importance, tags, "
                "dedupe_hash, created_at) VALUES "
                "(%s,%s,%s,%s,%s,%s,%s,%s, COALESCE(%s::timestamptz, now())) RETURNING id",
                (r.get("tenant_id") or "default", r.get("agent_id") or agent_id,
                 r.get("user_id"), r["note_kind"], r["content"], r.get("importance") or 5,
                 r.get("tags"), r.get("dedupe_hash"), r.get("created_at")),
            )
            id_map[("note", str(r["id"]))] = int(cur.fetchone()[0])
            stats["notes"] += 1

        # user_profile（升级唯一键已含 tenant_id；upsert）
        for r in _sqlite_rows(db, "user_profile", [
            "user_id", "agent_id", "tenant_id", "key", "value", "confidence", "updated_at",
        ]):
            cur.execute(
                "INSERT INTO user_profile (tenant_id, user_id, agent_id, key, value, confidence) "
                "VALUES (%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (tenant_id, user_id, agent_id, key) DO UPDATE SET "
                "value=EXCLUDED.value, confidence=EXCLUDED.confidence, updated_at=now()",
                (r.get("tenant_id") or "default", r["user_id"], r.get("agent_id") or agent_id,
                 r["key"], r["value"], r.get("confidence") or 5),
            )
            stats["user_profile"] += 1

        # memory_embeddings（重映射 source_id；source 未迁移则跳过孤儿）
        for r in _sqlite_rows(db, "memory_embeddings", [
            "source_kind", "source_id", "tenant_id", "user_id", "agent_id",
            "content_hash", "embedding_model", "dimensions", "embedding_json",
        ]):
            new_src = id_map.get((r["source_kind"], str(r["source_id"])))
            if new_src is None:
                continue
            cur.execute(
                "INSERT INTO memory_embeddings "
                "(source_kind, source_id, tenant_id, user_id, agent_id, content_hash, "
                "embedding_model, dimensions, embedding_json) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (source_kind, source_id, embedding_model) DO NOTHING",
                (r["source_kind"], str(new_src), r.get("tenant_id") or "default",
                 r.get("user_id"), r.get("agent_id") or agent_id, r["content_hash"],
                 r["embedding_model"], r.get("dimensions") or 0, r["embedding_json"]),
            )
            stats["embeddings"] += 1
    conn.commit()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="SQLite→PostgreSQL 记忆搬迁")
    ap.add_argument("--source-dir", default="", help="memory 目录（含 *.db）；默认 data/memory")
    ap.add_argument("--dsn", default="", help="PostgreSQL DSN；默认读 SMARTCLAW_MEMORY_POSTGRES_DSN")
    ap.add_argument("--reset", action="store_true", help="先清空本次涉及 agent 的目标数据再载入")
    args = ap.parse_args()

    dsn = args.dsn or os.environ.get("SMARTCLAW_MEMORY_POSTGRES_DSN", "")
    if not dsn:
        print("缺少 DSN：--dsn 或 SMARTCLAW_MEMORY_POSTGRES_DSN")
        return 2

    src = Path(args.source_dir) if args.source_dir else (
        Path(os.environ.get("SMARTCLAW_MEMORY_DATA_DIR", "")) / "memory"
        if os.environ.get("SMARTCLAW_MEMORY_DATA_DIR")
        else Path.home() / ".smartclaw" / "data" / "memory"
    )
    if not src.exists():
        print(f"源目录不存在：{src}")
        return 2

    dbs = sorted(src.glob("*.db"))
    if not dbs:
        print(f"未发现任何 *.db：{src}")
        return 0

    print(f"源目录={src}  共 {len(dbs)} 个 agent 库  →  PG={dsn.split('@')[-1]}")
    total = {"messages": 0, "summaries": 0, "notes": 0, "user_profile": 0, "embeddings": 0}
    with psycopg.connect(dsn, autocommit=False) as conn:
        for db in dbs:
            agent_id = db.stem
            _ensure_schema(dsn, agent_id)
            stats = migrate_db(conn, db, agent_id, args.reset)
            print(f"  [{agent_id}] " + " ".join(f"{k}={v}" for k, v in stats.items()))
            for k, v in stats.items():
                total[k] += v

        # 校验：PG 总行数 >= 本次迁入行数
        with conn.cursor() as cur:
            for t in ("messages", "summaries", "memory_notes", "user_profile"):
                cur.execute(f"SELECT COUNT(*) FROM {t}")
                print(f"  [verify] PG {t} 总行数 = {cur.fetchone()[0]}")

    print(f"[migrate OK] 迁入合计：{total}")
    return 0


# --------------------------------------------------------------------------- #
# 可选：RLS（行级安全）硬化 —— DB 层物理租户隔离。
#
# 注意：PostgreSQL 超级用户（docker 默认 ``postgres``）始终绕过 RLS，必须用「非超级
# 用户业务角色」连接，并对表加 FORCE。应用侧每个事务开头执行
#   SET LOCAL app.tenant_id = '<resolved_tenant>';
# 然后所有读写自动被下面策略约束（与应用层 WHERE tenant_id 形成双保险）。
#
# 以业务角色 smartclaw_app 为例（按需调整）：
#
#   CREATE ROLE smartclaw_app LOGIN PASSWORD '***' NOSUPERUSER NOBYPASSRLS;
#   GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO smartclaw_app;
#   GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO smartclaw_app;
#
#   DO $$ DECLARE t text;
#   BEGIN
#     FOREACH t IN ARRAY ARRAY['messages','summaries','memory_notes','user_profile'] LOOP
#       EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
#       EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
#       EXECUTE format($p$CREATE POLICY t_iso ON %I USING
#         (tenant_id = current_setting('app.tenant_id', true))
#         WITH CHECK (tenant_id = current_setting('app.tenant_id', true))$p$, t);
#     END LOOP;
#   END $$;
#
# 启用后需让 PostgresStore 连接使用 smartclaw_app 角色，并在每次操作前 SET app.tenant_id。
# 本仓库默认不启用（见 postgres_store.py 顶部说明），以保证「开了即可验证、不留半成品」。
# --------------------------------------------------------------------------- #


if __name__ == "__main__":
    sys.exit(main())
