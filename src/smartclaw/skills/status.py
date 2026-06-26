"""
Skill eligibility status builder.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import asdict
from typing import Any

from smartclaw.skills.loader import load_workspace_skill_entries
from smartclaw.skills.security import scan_skill_entry
from smartclaw.skills.types import SkillEntry, SkillStatus


def _resolve_skill_entry_config(config: Any, skill: SkillEntry) -> Any:
    skills = getattr(config, "skills", None)
    entries = getattr(skills, "entries", None) or {}
    key = skill.metadata.skill_key or skill.name
    return entries.get(key) or entries.get(skill.name)


def _is_env_satisfied(skill: SkillEntry, skill_config: Any, env_name: str) -> bool:
    if os.environ.get(env_name):
        return True
    if skill_config is None:
        return False
    cfg_env = getattr(skill_config, "env", None) or {}
    if cfg_env.get(env_name):
        return True
    primary_env = skill.metadata.primary_env
    if primary_env and primary_env == env_name and getattr(skill_config, "api_key", ""):
        return True
    return False


def _current_tenant_id(explicit_tenant_id: str | None = None) -> str:
    """Resolve tenant_id for skill status without making tool context mandatory."""
    if explicit_tenant_id:
        return explicit_tenant_id
    try:
        from smartclaw.auth.tool_gate import get_tool_security_context

        ctx = get_tool_security_context()
        if ctx and ctx.tenant_id:
            return ctx.tenant_id
    except Exception:
        return ""
    return ""


def _tenant_allowed(config: Any, skill: SkillEntry, skill_config: Any, tenant_id: str) -> bool:
    """Apply global and per-skill tenant allow/block policy."""
    if not tenant_id:
        return True
    skills_cfg = getattr(config, "skills", None)
    skill_key = skill.metadata.skill_key or skill.name
    global_allow = (getattr(skills_cfg, "tenant_allowlist_by_skill", None) or {}).get(skill_key, [])
    global_block = (getattr(skills_cfg, "tenant_blocklist_by_skill", None) or {}).get(skill_key, [])
    entry_allow = getattr(skill_config, "tenant_allowlist", None) or []
    entry_block = getattr(skill_config, "tenant_blocklist", None) or []

    allow = list(global_allow) + list(entry_allow)
    block = list(global_block) + list(entry_block)
    if tenant_id in block or "*" in block:
        return False
    if allow and tenant_id not in allow and "*" not in allow:
        return False
    return True


def build_workspace_skill_status(
    workspace_dir: str,
    config: Any = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Build skill eligibility rows for a workspace and optional tenant."""
    entries = load_workspace_skill_entries(workspace_dir, config=config)
    statuses: list[SkillStatus] = []
    runtime_env = getattr(getattr(config, "smartclaw", None), "environment", "development")
    resolved_tenant_id = _current_tenant_id(tenant_id)
    allowlist = set(getattr(getattr(config, "skills", None), "allowlist", []) or [])
    blocked_risk_levels = set(
        x.lower() for x in (getattr(getattr(config, "skills", None), "blocked_risk_levels", []) or [])
    )

    for skill in entries:
        skill_cfg = _resolve_skill_entry_config(config, skill)
        enabled = True
        if skill_cfg is not None and getattr(skill_cfg, "enabled", None) is False:
            enabled = False
        if allowlist and (skill.metadata.skill_key or skill.name) not in allowlist:
            enabled = False

        missing_bins = [b for b in skill.metadata.requires_bins if shutil.which(b) is None]
        missing_env = [
            e for e in skill.metadata.requires_env if not _is_env_satisfied(skill, skill_cfg, e)
        ]
        env_blocked = bool(skill.metadata.allowed_envs) and runtime_env not in skill.metadata.allowed_envs
        risk_blocked = skill.metadata.risk_level.lower() in blocked_risk_levels
        tenant_allowed = _tenant_allowed(config, skill, skill_cfg, resolved_tenant_id)

        max_file_bytes = getattr(
            getattr(getattr(config, "skills", None), "limits", None),
            "max_skill_file_bytes",
            256000,
        )
        security_findings = scan_skill_entry(skill, max_file_bytes=max_file_bytes)
        security_blocked = bool(security_findings) and (
            (skill.metadata.skill_key or skill.name)
            not in set(getattr(getattr(config, "skills", None), "security_allowlist_skill_keys", []) or [])
        )

        eligible = (
            enabled
            and not missing_bins
            and not missing_env
            and not env_blocked
            and not risk_blocked
            and not security_blocked
            and tenant_allowed
        )
        blocked_reason = None
        if not enabled:
            blocked_reason = "disabled_by_config"
        elif missing_bins:
            blocked_reason = "missing_bins"
        elif missing_env:
            blocked_reason = "missing_env"
        elif env_blocked:
            blocked_reason = "blocked_by_environment"
        elif risk_blocked:
            blocked_reason = "blocked_by_risk_policy"
        elif security_blocked:
            blocked_reason = "blocked_by_security_scan"
        elif not tenant_allowed:
            blocked_reason = "blocked_by_tenant_policy"

        statuses.append(
            SkillStatus(
                skill_key=skill.metadata.skill_key or skill.name,
                name=skill.name,
                source=skill.source,
                description=skill.description,
                version=skill.metadata.version,
                owner=skill.metadata.owner,
                reviewer=skill.metadata.reviewer,
                risk_level=skill.metadata.risk_level,
                test_status=skill.metadata.test_status,
                enabled=enabled,
                eligible=eligible,
                tenant_id=resolved_tenant_id,
                tenant_allowed=tenant_allowed,
                missing_bins=missing_bins,
                missing_env=missing_env,
                security_findings=security_findings,
                primary_env=skill.metadata.primary_env,
                blocked_reason=blocked_reason,
            )
        )

    return {
        "workspace_dir": workspace_dir,
        "tenant_id": resolved_tenant_id,
        "total": len(statuses),
        "eligible": sum(1 for s in statuses if s.eligible),
        "skills": [asdict(item) for item in statuses],
    }

