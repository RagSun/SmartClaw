"""
Skill scaffold creator.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def create_skill_scaffold(
    workspace_dir: str,
    *,
    name: str,
    description: str,
    owner: str,
    reviewer: str,
    risk_level: str,
    install_method: str,
    install_spec: str,
    requires_bins: list[str] | None = None,
    requires_env: list[str] | None = None,
) -> dict[str, Any]:
    skill_dir = Path(workspace_dir) / "skills" / name
    if skill_dir.exists():
        raise FileExistsError(f"skill already exists: {skill_dir}")

    (skill_dir / "scripts").mkdir(parents=True, exist_ok=False)
    (skill_dir / "tests").mkdir(parents=True, exist_ok=True)
    (skill_dir / "references").mkdir(parents=True, exist_ok=True)

    requires_bins = requires_bins or []
    requires_env = requires_env or []
    skill_md = f"""---
name: {name}
skill_key: {name}
version: 0.1.0
changelog: initial draft
owner: {owner}
reviewer: {reviewer}
sla: p2-2d
risk_level: {risk_level}
install_method: {install_method}
install_spec: {install_spec}
requires_bins: {", ".join(requires_bins)}
requires_env: {", ".join(requires_env)}
allowed_envs: development, staging
always: false
---

{description}

## Purpose
Describe what this skill does for the team.

## Input
- Required inputs
- Optional inputs

## Output
- Expected output shape

## Failure Modes
- Known failure scenarios

## Rollback
- How to disable or uninstall this skill safely.
"""
    (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")

    smoke = """import pathlib

def main() -> None:
    base = pathlib.Path(__file__).resolve().parents[1]
    skill_file = base / "SKILL.md"
    assert skill_file.exists(), "SKILL.md missing"
    text = skill_file.read_text(encoding="utf-8")
    assert "skill_key:" in text, "skill_key missing"
    assert "owner:" in text, "owner missing"
    print("smoke ok")

if __name__ == "__main__":
    main()
"""
    (skill_dir / "tests" / "smoke.py").write_text(smoke, encoding="utf-8")

    install_script = """#!/usr/bin/env bash
set -euo pipefail
echo "implement install steps"
"""
    (skill_dir / "scripts" / "install.sh").write_text(install_script, encoding="utf-8")

    return {
        "name": name,
        "skill_dir": str(skill_dir),
        "files": [
            str(skill_dir / "SKILL.md"),
            str(skill_dir / "tests" / "smoke.py"),
            str(skill_dir / "scripts" / "install.sh"),
        ],
    }
