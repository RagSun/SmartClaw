"""
Skill install/uninstall/repair lifecycle.
"""

from __future__ import annotations

import getpass
import shutil
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smartclaw.skills.governance import is_approved
from smartclaw.skills.loader import load_workspace_skill_entries
from smartclaw.skills.security import scan_skill
from smartclaw.skills.storage import read_registry, record_audit, record_event, write_registry
from smartclaw.skills.types import SkillEntry
from smartclaw.subprocess_io import SUBPROCESS_TEXT_KWARGS


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pick_entry(workspace_dir: str, name: str, config: Any = None) -> SkillEntry:
    for entry in load_workspace_skill_entries(workspace_dir, config=config):
        skill_key = entry.metadata.skill_key or entry.name
        if entry.name.lower() == name.lower() or skill_key.lower() == name.lower():
            return entry
    raise ValueError(f"skill not found: {name}")


def _run_command(command: list[str], cwd: str | None = None) -> tuple[bool, str]:
    result = subprocess.run(command, capture_output=True, text=True, cwd=cwd, **SUBPROCESS_TEXT_KWARGS)
    output = (result.stdout or "") + (("\n" + result.stderr) if result.stderr else "")
    return result.returncode == 0, output.strip()


def _install_by_method(entry: SkillEntry, config: Any = None) -> tuple[bool, str]:
    method = entry.metadata.install_method
    spec = entry.metadata.install_spec
    install_cfg = getattr(getattr(config, "skills", None), "install", None)
    node_manager = getattr(install_cfg, "node_manager", "npm")

    if method == "none":
        return True, "no install required"
    if method == "brew":
        return _run_command(["brew", "install", spec])
    if method == "node":
        return _run_command([node_manager, "install", "-g", spec])
    if method == "go":
        return _run_command(["go", "install", spec])
    if method == "uv":
        return _run_command(["uv", "tool", "install", spec])
    if method == "download":
        download_dir = Path.home() / ".smartclaw" / "downloads" / (entry.metadata.skill_key or entry.name)
        download_dir.mkdir(parents=True, exist_ok=True)
        filename = spec.rsplit("/", 1)[-1] or "artifact.bin"
        target = download_dir / filename
        urllib.request.urlretrieve(spec, target)  # noqa: S310
        return True, f"downloaded to {target}"
    if method == "custom":
        return _run_command(["powershell", "-NoProfile", "-Command", entry.metadata.install_command], cwd=entry.base_dir)
    return False, f"unsupported install method: {method}"


def _uninstall_by_method(entry: SkillEntry, config: Any = None) -> tuple[bool, str]:
    method = entry.metadata.install_method
    spec = entry.metadata.install_spec
    install_cfg = getattr(getattr(config, "skills", None), "install", None)
    node_manager = getattr(install_cfg, "node_manager", "npm")

    if method == "none":
        return True, "no uninstall required"
    if method == "brew":
        return _run_command(["brew", "uninstall", spec])
    if method == "node":
        return _run_command([node_manager, "uninstall", "-g", spec])
    if method == "uv":
        return _run_command(["uv", "tool", "uninstall", spec])
    if method == "download":
        download_dir = Path.home() / ".smartclaw" / "downloads" / (entry.metadata.skill_key or entry.name)
        if download_dir.exists():
            shutil.rmtree(download_dir)
        return True, f"removed {download_dir}"
    if method == "custom":
        return True, "custom uninstall not configured; skipped"
    if method == "go":
        return True, "go uninstall unsupported; please remove binary manually"
    return False, f"unsupported install method: {method}"


def install_skill(workspace_dir: str, name: str, config: Any = None, force: bool = False) -> dict[str, Any]:
    entry = _pick_entry(workspace_dir, name, config=config)
    skill_key = entry.metadata.skill_key or entry.name
    operator = getpass.getuser()

    sec = scan_skill(entry, config=config)
    if sec["blocked"] and not force:
        record_audit("install", skill_key, operator, "blocked", {"reason": "security_critical", "findings": sec["findings"]})
        return {"ok": False, "skill_key": skill_key, "blocked": True, "reason": "security_critical", "findings": sec["findings"]}

    approval_levels = set(getattr(getattr(config, "skills", None), "require_approval_for", []) or [])
    approved = is_approved(skill_key)
    if entry.metadata.risk_level in approval_levels and not (approved or force):
        return {"ok": False, "skill_key": skill_key, "blocked": True, "reason": "approval_required"}

    ok, logs = _install_by_method(entry, config=config)
    registry = read_registry()
    installed = registry.setdefault("installed", {})
    installed[skill_key] = {
        "name": entry.name,
        "version": entry.metadata.version,
        "method": entry.metadata.install_method,
        "spec": entry.metadata.install_spec,
        "status": "success" if ok else "failed",
        "last_operation": "install",
        "last_operation_at": _utc_now(),
        "logs": logs,
    }
    write_registry(registry)
    record_event("install", skill_key, {"ok": ok})
    record_audit("install", skill_key, operator, "success" if ok else "failed", {"logs": logs, "findings": sec["findings"]})
    return {"ok": ok, "skill_key": skill_key, "logs": logs, "findings": sec["findings"]}


def uninstall_skill(workspace_dir: str, name: str, config: Any = None) -> dict[str, Any]:
    entry = _pick_entry(workspace_dir, name, config=config)
    skill_key = entry.metadata.skill_key or entry.name
    operator = getpass.getuser()
    ok, logs = _uninstall_by_method(entry, config=config)

    registry = read_registry()
    installed = registry.setdefault("installed", {})
    item = installed.get(skill_key, {})
    item.update(
        {
            "status": "uninstalled" if ok else "uninstall_failed",
            "last_operation": "uninstall",
            "last_operation_at": _utc_now(),
            "logs": logs,
        }
    )
    installed[skill_key] = item
    write_registry(registry)

    record_event("uninstall", skill_key, {"ok": ok})
    record_audit("uninstall", skill_key, operator, "success" if ok else "failed", {"logs": logs})
    return {"ok": ok, "skill_key": skill_key, "logs": logs}


def repair_skill(workspace_dir: str, name: str, config: Any = None, force: bool = False) -> dict[str, Any]:
    remove = uninstall_skill(workspace_dir, name, config=config)
    install = install_skill(workspace_dir, name, config=config, force=force)
    return {"ok": remove["ok"] and install["ok"], "uninstall": remove, "install": install}


def reinstall_skill(workspace_dir: str, name: str, config: Any = None, force: bool = False) -> dict[str, Any]:
    return repair_skill(workspace_dir, name, config=config, force=force)
