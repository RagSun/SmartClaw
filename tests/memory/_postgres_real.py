"""真实 PostgreSQL 测试夹具（无 mock、无回退）。

未设置 ``SMARTCLAW_TEST_POSTGRES_DSN`` 或 PG 不可达 → friendly skip。
测试统一用 ``pgtest_`` 前缀的 agent_id，便于清理只删自己的数据。
"""

from __future__ import annotations

import os
import uuid

import pytest

AGENT_PREFIX = "pgtest_"


def dsn_or_skip() -> str:
    dsn = os.environ.get("SMARTCLAW_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("未设置 SMARTCLAW_TEST_POSTGRES_DSN，跳过真实 PostgreSQL 测试")
    try:
        import psycopg  # noqa: F401
    except ImportError:
        pytest.skip("psycopg 未安装（uv pip install 'psycopg[binary,pool]'）")
    try:
        import psycopg

        conn = psycopg.connect(dsn, connect_timeout=3)
        conn.close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL 不可达（{dsn.split('@')[-1]}）：{exc}")
    return dsn


def new_agent() -> str:
    return AGENT_PREFIX + uuid.uuid4().hex[:10]


def cleanup(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        for table in (
            "memory_embeddings", "messages", "summaries", "memory_notes", "user_profile",
        ):
            try:
                cur.execute(f"DELETE FROM {table} WHERE agent_id LIKE %s", (f"{AGENT_PREFIX}%",))
            except Exception:  # noqa: BLE001
                pass
