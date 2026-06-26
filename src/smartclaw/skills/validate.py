"""
Skill schema and structure validation.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from smartclaw.skills.loader import load_workspace_skill_entries
from smartclaw.skills.types import SkillEntry

VALID_RISK_LEVELS = {"info", "warn", "high", "critical"}
VALID_INSTALL_METHODS = {"none", "brew", "node", "go", "uv", "download", "custom"}
SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _validate_skill(entry: SkillEntry) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    md = entry.metadata
    skill_key = md.skill_key or entry.name

    if not SKILL_NAME_RE.match(skill_key):
        errors.append("skill_key must match ^[a-z0-9][a-z0-9_-]{1,63}$")
    if not SEMVER_RE.match(md.version):
        errors.append("version must be semver, e.g. 1.2.3")
    if not md.owner:
        errors.append("owner is required")
    if not md.reviewer:
        errors.append("reviewer is required")
    if not md.sla:
        warnings.append("sla is empty")
    if md.risk_level not in VALID_RISK_LEVELS:
        errors.append(f"risk_level must be one of {sorted(VALID_RISK_LEVELS)}")
    if md.install_method not in VALID_INSTALL_METHODS:
        errors.append(f"install_method must be one of {sorted(VALID_INSTALL_METHODS)}")
    if md.install_method in {"brew", "node", "go", "uv", "download"} and not md.install_spec:
        errors.append("install_spec is required for selected install_method")
    if md.install_method == "custom" and not md.install_command:
        errors.append("install_command is required for custom install_method")

    base = Path(entry.base_dir)
    for dirname in ("scripts", "tests", "references"):
        if not (base / dirname).exists():
            errors.append(f"missing required directory: {dirname}/")

    return errors, warnings


def validate_workspace_skills(workspace_dir: str, config: Any = None) -> dict[str, Any]:
    entries = load_workspace_skill_entries(workspace_dir, config=config)
    report: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    global_errors: list[str] = []

    for entry in entries:
        skill_key = entry.metadata.skill_key or entry.name
        if skill_key in seen:
            global_errors.append(f"duplicate skill_key: {skill_key} ({seen[skill_key]} vs {entry.base_dir})")
        else:
            seen[skill_key] = entry.base_dir

        errors, warnings = _validate_skill(entry)
        report.append(
            {
                "skill_key": skill_key,
                "name": entry.name,
                "path": entry.base_dir,
                "errors": errors,
                "warnings": warnings,
                "ok": not errors,
            }
        )

    return {
        "workspace_dir": workspace_dir,
        "total": len(report),
        "ok": sum(1 for item in report if item["ok"]) == len(report) and not global_errors,
        "global_errors": global_errors,
        "skills": report,
    }


def lint_workspace_skills(workspace_dir: str, config: Any = None) -> dict[str, Any]:
    entries = load_workspace_skill_entries(workspace_dir, config=config)
    skills: list[dict[str, Any]] = []
    lint_errors = 0

    for entry in entries:
        issues: list[str] = []
        if len(entry.description.strip()) < 20:
            issues.append("description too short (<20 chars)")
        if entry.name.lower() != entry.name:
            issues.append("skill display name should be lowercase for consistency")
        if "todo" in entry.description.lower():
            issues.append("description contains TODO placeholder")

        skills.append(
            {
                "skill_key": entry.metadata.skill_key or entry.name,
                "name": entry.name,
                "issues": issues,
                "ok": not issues,
            }
        )
        lint_errors += len(issues)

    return {
        "workspace_dir": workspace_dir,
        "total": len(skills),
        "ok": lint_errors == 0,
        "issue_count": lint_errors,
        "skills": skills,
    }
