"""SQLite FTS5 记忆检索与上下文注入。"""

import tempfile
from pathlib import Path

from smartclaw.memory.manager import MemoryManager


def test_fts_injects_retrieval_when_query_matches() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        mm = MemoryManager(
            agent_id="fts_agent",
            session_id="s_fts",
            channel="feishu",
            user_id="u_fts",
            data_dir=Path(tmp),
        )
        mm.tenant_id = "default"
        try:
            if not mm._store._fts_ready:
                return
            mm.add_message("user", "we use beta stack for alpha milestone")
            mm.add_message("assistant", "noted.")
            mm._store.add_memory_note(
                "note",
                "beta stack uses sqlite fts5",
                importance=5,
                user_id="u_fts",
                agent_id="fts_agent",
            )
            ctx = mm.get_context_for_llm(
                retrieval_query="sqlite",
                fts_top_k=5,
            )
            blocks = "\n".join(m.get("content", "") for m in ctx)
            assert "[记忆检索]" in blocks
            assert "sqlite" in blocks.lower()
        finally:
            mm.close()


def test_fts_phrase_too_short_returns_no_retrieval_block() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        mm = MemoryManager(
            agent_id="fts2",
            session_id="s2",
            channel="feishu",
            user_id="u2",
            data_dir=Path(tmp),
        )
        try:
            ctx = mm.get_context_for_llm(retrieval_query="x", fts_top_k=3)
            assert not any(
                (m.get("content") or "").startswith("[记忆检索]") for m in ctx
            )
        finally:
            mm.close()
