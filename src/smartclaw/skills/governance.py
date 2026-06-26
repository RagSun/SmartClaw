"""
Skill governance: env approval, promote, rollback, deprecate.
"""

from __future__ import annotations

import getpass
from datetime import datetime, timezone
from typing import Any

from smartclaw.skills.loader import load_workspace_skill_entries
from smartclaw.skills.storage import (
    read_approvals,
    read_releases,
    record_audit,
    record_event,
    write_approvals,
    write_releases,
)
from smartclaw.skills.types import SkillEntry

ENV_ORDER = ["development", "staging", "production"]
REQUIRES_ENV_APPROVAL = {"staging", "production"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pick_entry(workspace_dir: str, name: str, config: Any = None) -> SkillEntry:
    for entry in load_workspace_skill_entries(workspace_dir, config=config):
        skill_key = entry.metadata.skill_key or entry.name
        if name.lower() in {entry.name.lower(), skill_key.lower()}:
            return entry
    raise ValueError(f"skill not found: {name}")


def _normalize_env(env: str) -> str:
    if env not in ENV_ORDER:
        raise ValueError("env must be one of development/staging/production")
    return env


def approve_skill(
    workspace_dir: str,
    name: str,
    env: str = "staging",
    note: str = "",
    config: Any = None,
) -> dict[str, Any]:
    entry = _pick_entry(workspace_dir, name, config=config)
    skill_key = entry.metadata.skill_key or entry.name
    env = _normalize_env(env)
    operator = getpass.getuser()
    approvals = read_approvals()
    rows = approvals.setdefault("approvals", {})
    skill_rows = rows.setdefault(skill_key, {})
    skill_rows[env] = {
        "approved": True,
        "operator": operator,
        "note": note,
        "approved_at": _utc_now(),
    }
    rows[skill_key] = skill_rows
    write_approvals(approvals)
    record_event("approve", skill_key, {"operator": operator, "env": env, "note": note})
    record_audit("approve", skill_key, operator, "success", {"env": env, "note": note})
    return {"ok": True, "skill_key": skill_key, "env": env, "approved": True}


def is_approved(skill_key: str, env: str | None = None) -> bool:
    approvals = read_approvals().get("approvals", {})
    row = approvals.get(skill_key, {})
    # backward compatible: old schema {"approved": true, ...}
    if isinstance(row, dict) and "approved" in row:
        return bool(row.get("approved", False))
    if env is None:
        return any(bool(v.get("approved", False)) for v in row.values() if isinstance(v, dict))
    return bool(row.get(env, {}).get("approved", False))


def promote_skill(workspace_dir: str, name: str, env: str, note: str = "", config: Any = None) -> dict[str, Any]:
    env = _normalize_env(env)
    entry = _pick_entry(workspace_dir, name, config=config)
    skill_key = entry.metadata.skill_key or entry.name
    operator = getpass.getuser()

    releases = read_releases()
    by_skill = releases.setdefault("releases", {})
    item = by_skill.setdefault(skill_key, {})
    current_env = item.get("current_env")

    if current_env is None:
        if env != "development":
            return {
                "ok": False,
                "skill_key": skill_key,
                "reason": "invalid_promotion_order",
                "message": "first promotion must target development",
            }
    else:
        if current_env not in ENV_ORDER:
            return {"ok": False, "skill_key": skill_key, "reason": "invalid_release_state"}
        current_idx = ENV_ORDER.index(current_env)
        target_idx = ENV_ORDER.index(env)
        if target_idx == current_idx:
            return {"ok": False, "skill_key": skill_key, "reason": "already_in_target_env"}
        if target_idx < current_idx:
            return {
                "ok": False,
                "skill_key": skill_key,
                "reason": "target_is_lower_env_use_rollback",
            }
        if target_idx != current_idx + 1:
            return {
                "ok": False,
                "skill_key": skill_key,
                "reason": "invalid_promotion_order",
                "message": f"must follow {' -> '.join(ENV_ORDER)}",
            }

    if env in REQUIRES_ENV_APPROVAL and not is_approved(skill_key, env):
        return {"ok": False, "skill_key": skill_key, "reason": f"approval_required_for_{env}"}

    history = item.setdefault("history", [])
    history.append(
        {
            "action": "promote",
            "env": env,
            "version": entry.metadata.version,
            "operator": operator,
            "note": note,
            "timestamp": _utc_now(),
        }
    )
    item["current_env"] = env
    item["current_version"] = entry.metadata.version
    by_skill[skill_key] = item
    write_releases(releases)

    record_event("promote", skill_key, {"env": env, "version": entry.metadata.version})
    record_audit("promote", skill_key, operator, "success", {"env": env, "note": note})
    return {
        "ok": True,
        "skill_key": skill_key,
        "env": env,
        "version": entry.metadata.version,
    }


def rollback_skill(workspace_dir: str, name: str, to_env: str, note: str = "", config: Any = None) -> dict[str, Any]:
    to_env = _normalize_env(to_env)
    entry = _pick_entry(workspace_dir, name, config=config)
    skill_key = entry.metadata.skill_key or entry.name
    operator = getpass.getuser()

    releases = read_releases()
    by_skill = releases.setdefault("releases", {})
    item = by_skill.setdefault(skill_key, {})
    current_env = item.get("current_env")
    if current_env is None:
        return {"ok": False, "skill_key": skill_key, "reason": "no_release_state"}
    if current_env not in ENV_ORDER:
        return {"ok": False, "skill_key": skill_key, "reason": "invalid_release_state"}

    current_idx = ENV_ORDER.index(current_env)
    target_idx = ENV_ORDER.index(to_env)
    if target_idx >= current_idx:
        return {"ok": False, "skill_key": skill_key, "reason": "rollback_must_target_lower_env"}

    history = item.setdefault("history", [])
    rollback_version = item.get("current_version", entry.metadata.version)
    for row in reversed(history):
        if row.get("env") == to_env and row.get("version"):
            rollback_version = row["version"]
            break

    history.append(
        {
            "action": "rollback",
            "from_env": current_env,
            "env": to_env,
            "version": rollback_version,
            "operator": operator,
            "note": note,
            "timestamp": _utc_now(),
        }
    )
    item["current_env"] = to_env
    item["current_version"] = rollback_version
    by_skill[skill_key] = item
    write_releases(releases)

    record_event("rollback", skill_key, {"from_env": current_env, "to_env": to_env, "version": rollback_version})
    record_audit("rollback", skill_key, operator, "success", {"from_env": current_env, "to_env": to_env, "note": note})
    return {
        "ok": True,
        "skill_key": skill_key,
        "from_env": current_env,
        "to_env": to_env,
        "version": rollback_version,
    }


def deprecate_skill(workspace_dir: str, name: str, reason: str, config: Any = None) -> dict[str, Any]:
    entry = _pick_entry(workspace_dir, name, config=config)
    skill_key = entry.metadata.skill_key or entry.name
    operator = getpass.getuser()

    releases = read_releases()
    by_skill = releases.setdefault("releases", {})
    item = by_skill.setdefault(skill_key, {})
    item["deprecated"] = True
    item["deprecated_reason"] = reason
    item["deprecated_by"] = operator
    item["deprecated_at"] = _utc_now()
    by_skill[skill_key] = item
    write_releases(releases)

    record_event("deprecate", skill_key, {"reason": reason})
    record_audit("deprecate", skill_key, operator, "success", {"reason": reason})
    return {"ok": True, "skill_key": skill_key, "deprecated": True, "reason": reason}
