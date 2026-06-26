"""记忆上下文拼装辅助：避免 compact 模式误用 memory_context[0]（首条可能是长期记忆而非摘要）。"""

from __future__ import annotations

from typing import Any

# compact 模式下需要保留的记忆 system 块前缀。必须与 manager.get_context_for_llm
# 实际注入的标签保持一致——遗漏任一前缀会导致该层在 compact 模式被静默丢弃。
# [团队知识]/[我的记忆] 为长期记忆双层化后的标签；[用户记忆] 为历史单层标签（向后兼容保留）。
_MEMORY_PREFIXES = (
    "[团队知识]",
    "[我的记忆]",
    "[用户记忆]",
    "[对话摘要]",
    "[记忆检索]",
)


def compact_prefix_from_memory_context(
    memory_context: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    提取仅用于 compact 注入的 system 条：与 get_context_for_llm(..., include_stored_transcript=False)
    中注入的长期记忆/摘要/检索块一致，保持原有顺序。

    若不存在对应前缀，返回空列表。
    """
    out: list[dict[str, Any]] = []
    for m in memory_context:
        if m.get("role") != "system":
            continue
        c = (m.get("content") or "").strip()
        if c.startswith(_MEMORY_PREFIXES):
            out.append(m)
    return out


__all__ = ["compact_prefix_from_memory_context"]
