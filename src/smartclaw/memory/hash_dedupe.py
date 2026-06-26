"""记忆要点与晋升内容的稳定哈希去重（空格规范化后同一语义合并为同一键）。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional


def _norm_text(s: str) -> str:
    return " ".join((s or "").strip().split())


def note_row_hash(
    *,
    note_kind: str,
    content: str,
    user_id: Optional[str],
    agent_id: Optional[str],
) -> str:
    """与 memory_notes 表 dedupe_hash 一致：kind + 规范化正文 + user + agent。

    注意：payload 公式与历史 ``event_row_hash`` 完全一致（首段仍为 kind 值），
    故旧库已存的 dedupe_hash 继续命中，升级无需重算。
    """
    payload = "\n".join(
        [
            (note_kind or "").strip(),
            _norm_text(content),
            user_id or "",
            agent_id or "",
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def promote_entry_hash(
    *,
    user_id: str,
    agent_id: str,
    note_kind: str,
    content: str,
) -> str:
    """晋升到 MEMORY.md 的去重键（与用户/代理绑定）。

    payload 公式与历史保持一致，保证 promoted_hashes.sha256 旧指纹继续有效。
    """
    payload = "\n".join(
        [
            user_id or "",
            agent_id or "",
            (note_kind or "").strip(),
            _norm_text(content),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_hash_set(path: Path) -> set[str]:
    if not path.exists():
        return set()
    text = path.read_text(encoding="utf-8", errors="replace")
    return {line.strip() for line in text.splitlines() if line.strip()}


def append_hash(path: Path, digest: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(digest + "\n")
