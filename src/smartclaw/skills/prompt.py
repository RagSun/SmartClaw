"""
Build skills context prompt for runner/react.
"""

from __future__ import annotations

from typing import Any

from smartclaw.skills.loader import load_workspace_skill_entries
from smartclaw.skills.status import build_workspace_skill_status


def build_skills_system_prompt(workspace_dir: str, config: Any = None) -> tuple[str, list[str]]:
    status = build_workspace_skill_status(workspace_dir, config=config)
    entries: dict[str, Any] = {}
    for entry in load_workspace_skill_entries(workspace_dir, config=config):
        entries[entry.name] = entry
        entries[entry.metadata.skill_key or entry.name] = entry
    eligible = [s for s in status["skills"] if s.get("eligible")]
    if not eligible:
        return "", []

    limits = getattr(getattr(config, "skills", None), "limits", None)
    max_count = int(getattr(limits, "max_skills_in_prompt", 80) or 80)
    max_chars = int(getattr(limits, "max_skills_prompt_chars", 20000) or 20000)

    chosen = eligible[:max_count]
    lines = [
        "You can use the following approved team skills when helpful.",
        "Prefer these standardized skills before ad-hoc tool calls.",
        "",
    ]
    included: list[str] = []
    for row in chosen:
        skill_key = row.get("skill_key") or row.get("name")
        entry = entries.get(skill_key)
        if entry is None:
            continue
        meta = entry.metadata
        lines.append(
            f"- {meta.skill_key or entry.name}: {entry.description} | owner={meta.owner or '-'} | "
            f"risk={meta.risk_level} | version={meta.version}"
        )
        included.append(meta.skill_key or entry.name)
        text = "\n".join(lines)
        if len(text) > max_chars:
            lines.pop()
            included.pop()
            break

    return "\n".join(lines), included
