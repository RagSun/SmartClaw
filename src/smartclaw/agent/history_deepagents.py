"""DeepAgents 输入历史裁剪：去掉与 Session 重复的 SQLite transcript、去掉重复的 skills system 片段。"""

from __future__ import annotations

from typing import Any

MAX_DEEPAGENTS_HISTORY = 20


def without_skills_system_duplicate(
    history: list[dict[str, Any]],
    skills_prompt: str | None,
) -> list[dict[str, Any]]:
    """ skills 已编译进 DeepAgents 的 system_prompt 时，去掉 history 里同内容的 system 条，避免重复占窗。 """
    if not history:
        return history
    sp = (skills_prompt or "").strip()
    if not sp:
        return list(history)
    out: list[dict[str, Any]] = []
    for m in history:
        if m.get("role") == "system" and (m.get("content") or "").strip() == sp:
            continue
        out.append(m)
    return out


def clip_history_for_deepagents(
    history: list[dict[str, Any]],
    skills_prompt: str | None,
    max_messages: int = MAX_DEEPAGENTS_HISTORY,
) -> list[dict[str, Any]]:
    trimmed = without_skills_system_duplicate(history, skills_prompt)
    if len(trimmed) > max_messages:
        return trimmed[-max_messages:]
    return trimmed


__all__ = [
    "MAX_DEEPAGENTS_HISTORY",
    "without_skills_system_duplicate",
    "clip_history_for_deepagents",
]
