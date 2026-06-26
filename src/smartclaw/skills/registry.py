"""
SkillRegistry — 运行时技能视图：prompt 注入 + 结构化元数据供 Planner / 观测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from smartclaw.skills.prompt import build_skills_system_prompt
from smartclaw.skills.loader import load_workspace_skill_entries
from smartclaw.skills.status import build_workspace_skill_status


@dataclass
class SkillRuntimeView:
    skills_prompt: str
    included_keys: list[str]
    eligible_skills: list[dict[str, Any]] = field(default_factory=list)


class SkillRegistry:
    """_workspace_dir 下 SKILLS 的运行时注册与可执行能力摘要。"""

    def __init__(self, workspace_dir: str, config: Any = None):
        self.workspace_dir = workspace_dir
        self.config = config

    def build(self) -> SkillRuntimeView:
        status = build_workspace_skill_status(self.workspace_dir, config=self.config)
        entries: dict[str, Any] = {}
        for entry in load_workspace_skill_entries(self.workspace_dir, config=self.config):
            entries[entry.name] = entry
            if entry.metadata.skill_key:
                entries[entry.metadata.skill_key] = entry

        prompt, included = build_skills_system_prompt(self.workspace_dir, config=self.config)
        eligible_rows = [s for s in status.get("skills", []) if s.get("eligible")]
        eligible_dicts: list[dict[str, Any]] = []
        for row in eligible_rows:
            key = row.get("skill_key") or row.get("name")
            ent = entries.get(key or "")
            meta = getattr(ent, "metadata", None) if ent else None
            eligible_dicts.append(
                {
                    "skill_key": key,
                    "name": row.get("name"),
                    "risk_level": getattr(meta, "risk_level", None) if meta else row.get("risk_level"),
                    "version": getattr(meta, "version", None) if meta else row.get("version"),
                    "owner": getattr(meta, "owner", None) if meta else row.get("owner"),
                }
            )
        return SkillRuntimeView(
            skills_prompt=prompt,
            included_keys=included,
            eligible_skills=eligible_dicts,
        )
