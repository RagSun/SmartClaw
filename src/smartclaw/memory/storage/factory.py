"""记忆存储后端工厂（见 progress.md §12）。

沿用 governance/store 与 tenancy/registry 的「工厂选后端」套路：
- ``sqlite``（默认）：按 agent 分文件 ``{data_dir}/memory/{agent_id}.db``，单机/共享卷部署。
- ``postgres``：共享库，多实例一致（同一对话可由任意副本处理）。

调用方（``MemoryManager``）只依赖 ``MemoryStore`` 协议，切后端零改动。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from smartclaw.console import info, warning


@runtime_checkable
class MemoryStore(Protocol):
    """记忆数据面后端契约（SQLiteStore / PostgresStore 均实现）。

    方法签名与 ``SQLiteStore`` 完全一致，便于透明替换。这里只列出
    ``MemoryManager`` 实际依赖的公共方法。
    """

    _fts_ready: bool

    def initialize(self) -> None: ...

    def add_message(
        self, session_id: str, role: str, content: str, tokens: int = ...,
        tool_name: Optional[str] = ..., tool_call_id: Optional[str] = ...,
        tenant_id: str = ...,
    ) -> int: ...

    def get_messages(
        self, session_id: str, limit: int = ..., offset: int = ..., tenant_id: str = ...,
    ) -> list[dict[str, Any]]: ...

    def get_message_count(self, session_id: str, tenant_id: str = ...) -> int: ...

    def add_summary(
        self, session_id: str, summary: str, original_count: int,
        summary_type: str = ..., tenant_id: str = ...,
    ) -> int: ...

    def get_latest_summary(
        self, session_id: str, tenant_id: str = ...,
    ) -> Optional[dict[str, Any]]: ...

    def get_summaries(
        self, session_id: str, limit: int = ..., tenant_id: str = ...,
    ) -> list[dict[str, Any]]: ...

    def add_memory_note(
        self, note_kind: str, content: str, importance: int = ...,
        tags: Optional[str] = ..., user_id: Optional[str] = ...,
        agent_id: Optional[str] = ..., *, dedupe: bool = ..., tenant_id: str = ...,
    ) -> int: ...

    def get_memory_notes(
        self, user_id: Optional[str] = ..., agent_id: Optional[str] = ...,
        note_kind: Optional[str] = ..., limit: int = ..., tenant_id: str = ...,
    ) -> list[dict[str, Any]]: ...

    def note_exists_by_dedupe_hash(
        self, dedupe_hash: str, user_id: Optional[str] = ...,
        agent_id: Optional[str] = ..., tenant_id: str = ...,
    ) -> bool: ...

    def set_profile(
        self, user_id: str, agent_id: str, key: str, value: str,
        confidence: int = ..., tenant_id: str = ...,
    ) -> None: ...

    def get_profile(
        self, user_id: str, agent_id: str, tenant_id: str = ...,
    ) -> dict[str, str]: ...

    def search_memory_fts(self, **kwargs: Any) -> list[dict[str, Any]]: ...

    def get_memory_source_records(self, **kwargs: Any) -> list[dict[str, Any]]: ...

    def get_memory_record(self, **kwargs: Any) -> Optional[dict[str, Any]]: ...

    def get_embedding(self, **kwargs: Any) -> Optional[dict[str, Any]]: ...

    def upsert_embedding(self, **kwargs: Any) -> None: ...

    def vacuum_if_needed(self) -> None: ...

    def close(self) -> None: ...


def create_memory_store(
    *,
    agent_id: str,
    memory_subdir: Path,
    store: str = "sqlite",
    postgres_dsn: str = "",
) -> MemoryStore:
    """按配置返回记忆后端实例。

    - ``store="postgres"``：需 ``postgres_dsn``；连不上时 fail-fast（绝不静默回退到本地
      SQLite，避免「以为在用共享库、实际各写各的」的隐蔽不一致）。
    - 其他：默认 SQLite，落 ``{memory_subdir}/{agent_id}.db``。
    """
    backend = (store or "sqlite").strip().lower()
    if backend in ("postgres", "postgresql", "pg"):
        if not postgres_dsn.strip():
            raise ValueError(
                "memory.store=postgres 但未提供 postgres_dsn（或 SMARTCLAW_MEMORY_POSTGRES_DSN）"
            )
        from smartclaw.memory.storage.postgres_store import PostgresStore

        info(f"[memory] 使用 PostgreSQL 共享后端（agent={agent_id}）")
        st = PostgresStore(dsn=postgres_dsn, agent_id=agent_id)
        st.initialize()
        return st

    if backend not in ("sqlite", ""):
        warning(f"[memory] 未知 store={store!r}，回退 sqlite")
    from smartclaw.memory.storage.sqlite_store import SQLiteStore

    db_path = memory_subdir / f"{agent_id}.db"
    st = SQLiteStore(db_path)
    st.initialize()
    return st
