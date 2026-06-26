"""
Skill discovery and SKILL.md parsing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import smartclaw.paths as paths

from smartclaw.skills.types import SkillEntry, SkillMetadata


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_frontmatter_and_description(raw: str) -> tuple[dict[str, str], str]:
    frontmatter: dict[str, str] = {}
    body = raw
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) == 3:
            block = parts[1]
            body = parts[2]
            for line in block.splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                key = key.strip().lower().replace("-", "_")
                frontmatter[key] = value.strip()

    description = ""
    for line in body.splitlines():
        text = line.strip()
        if not text:
            continue
        if text.startswith("#"):
            continue
        description = text
        break
    return frontmatter, description


def _build_metadata(frontmatter: dict[str, str], fallback_name: str) -> SkillMetadata:
    return SkillMetadata(
        skill_key=frontmatter.get("skill_key", fallback_name),
        version=frontmatter.get("version", "0.1.0"),
        changelog=frontmatter.get("changelog", ""),
        primary_env=frontmatter.get("primary_env"),
        owner=frontmatter.get("owner", ""),
        reviewer=frontmatter.get("reviewer", ""),
        sla=frontmatter.get("sla", ""),
        risk_level=frontmatter.get("risk_level", "info").lower(),
        test_status=frontmatter.get("test_status", "unknown").lower(),
        install_method=frontmatter.get("install_method", "none").lower(),
        install_spec=frontmatter.get("install_spec", ""),
        install_command=frontmatter.get("install_command", ""),
        allowed_envs=_parse_csv(frontmatter.get("allowed_envs", "")),
        requires_bins=_parse_csv(frontmatter.get("requires_bins", "")),
        requires_env=_parse_csv(frontmatter.get("requires_env", "")),
        always=_parse_bool(frontmatter.get("always", "false")),
    )


def _iter_skill_dirs(root_dir: Path) -> list[Path]:
    if not root_dir.exists() or not root_dir.is_dir():
        return []
    skill_dirs: list[Path] = []
    for child in root_dir.iterdir():
        if not child.is_dir():
            continue
        if (child / "SKILL.md").exists():
            skill_dirs.append(child)
    return sorted(skill_dirs, key=lambda p: p.name.lower())


def _load_from_root(root_dir: Path, source: str) -> list[SkillEntry]:
    entries: list[SkillEntry] = []
    for skill_dir in _iter_skill_dirs(root_dir):
        skill_md = skill_dir / "SKILL.md"
        try:
            raw = skill_md.read_text(encoding="utf-8")
        except Exception:
            continue
        frontmatter, description = _parse_frontmatter_and_description(raw)
        name = frontmatter.get("name", skill_dir.name).strip() or skill_dir.name
        entries.append(
            SkillEntry(
                name=name,
                description=description or f"{name} skill",
                source=source,
                skill_md_path=str(skill_md),
                base_dir=str(skill_dir),
                metadata=_build_metadata(frontmatter, name),
            )
        )
    return entries


def _resolve_extra_dirs(config: Any) -> list[Path]:
    extra_dirs: list[Path] = []
    skills = getattr(config, "skills", None)
    load = getattr(skills, "load", None)
    configured = getattr(load, "extra_dirs", None) or []
    for item in configured:
        if not isinstance(item, str) or not item.strip():
            continue
        extra_dirs.append(Path(item).expanduser())
    return extra_dirs


def load_workspace_skill_entries(workspace_dir: str, config: Any = None) -> list[SkillEntry]:
    """
    Discover skills with deterministic precedence:
    extra < managed < workspace.
    """
    workspace = Path(workspace_dir).resolve()
    managed_dir = (paths.USER_HOME / "skills").resolve()
    workspace_skills_dir = (workspace / "skills").resolve()

    discovered: list[SkillEntry] = []
    extra_dirs = _resolve_extra_dirs(config)
    for extra_dir in extra_dirs:
        discovered.extend(_load_from_root(extra_dir, "smartclaw-extra"))

    discovered.extend(_load_from_root(managed_dir, "smartclaw-managed"))
    discovered.extend(_load_from_root(workspace_skills_dir, "smartclaw-workspace"))

    merged: dict[str, SkillEntry] = {}
    for item in discovered:
        key = item.metadata.skill_key or item.name
        merged[key] = item
    return sorted(merged.values(), key=lambda s: s.name.lower())

