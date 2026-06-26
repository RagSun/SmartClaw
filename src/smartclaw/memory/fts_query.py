"""FTS5 查询短语构造（避免特殊字符破 MATCH 语法）。"""

from __future__ import annotations


from typing import Optional


def fts5_phrase_query(raw: str, *, max_chars: int = 320) -> Optional[str]:
    """
    将用户原句包成 FTS5 短语 \"...\"；过长截断。
    返回 None 表示不宜发起 FTS（过短）。
    """
    t = (raw or "").strip()
    if len(t) < 2:
        return None
    t = t[:max_chars]
    t = t.replace('"', '""')
    return f'"{t}"'
