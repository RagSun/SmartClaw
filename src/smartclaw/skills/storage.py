"""
Skill state, audit, and snapshot persistence.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import smartclaw.paths as paths

from smartclaw.skills.types import SkillEntry


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_state_dir() -> Path:
    state_dir = paths.USER_HOME / "skills-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def get_registry_file() -> Path:
    return get_state_dir() / "registry.json"


def get_events_file() -> Path:
    return get_state_dir() / "events.jsonl"


def get_audit_file() -> Path:
    return get_state_dir() / "audit.jsonl"


def get_snapshot_file() -> Path:
    return get_state_dir() / "snapshot.json"


def get_approvals_file() -> Path:
    return get_state_dir() / "approvals.json"


def get_releases_file() -> Path:
    return get_state_dir() / "releases.json"


def read_registry() -> dict[str, Any]:
    file_path = get_registry_file()
    if not file_path.exists():
        return {"installed": {}, "updated_at": None}
    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except Exception:
        return {"installed": {}, "updated_at": None}


def write_registry(payload: dict[str, Any]) -> None:
    payload["updated_at"] = _utc_now()
    get_registry_file().write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _read_json_with_default(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_approvals() -> dict[str, Any]:
    return _read_json_with_default(get_approvals_file(), {"approvals": {}, "updated_at": None})


def write_approvals(payload: dict[str, Any]) -> None:
    payload["updated_at"] = _utc_now()
    _write_json(get_approvals_file(), payload)


def read_releases() -> dict[str, Any]:
    return _read_json_with_default(get_releases_file(), {"releases": {}, "updated_at": None})


def write_releases(payload: dict[str, Any]) -> None:
    payload["updated_at"] = _utc_now()
    _write_json(get_releases_file(), payload)


def append_jsonl(file_path: Path, payload: dict[str, Any]) -> None:
    payload = dict(payload)
    payload["timestamp"] = _utc_now()
    with file_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def record_event(event_type: str, skill_key: str, data: dict[str, Any] | None = None) -> None:
    append_jsonl(
        get_events_file(),
        {"event_type": event_type, "skill_key": skill_key, "data": data or {}},
    )


def record_audit(action: str, skill_key: str, operator: str, result: str, data: dict[str, Any] | None = None) -> None:
    append_jsonl(
        get_audit_file(),
        {
            "action": action,
            "skill_key": skill_key,
            "operator": operator,
            "result": result,
            "data": data or {},
        },
    )


def build_snapshot(entries: list[SkillEntry]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    hasher = hashlib.sha1()
    for entry in sorted(entries, key=lambda e: (e.metadata.skill_key or e.name).lower()):
        skill_key = entry.metadata.skill_key or entry.name
        md_file = Path(entry.skill_md_path)
        mtime = 0.0
        if md_file.exists():
            mtime = md_file.stat().st_mtime
        row = {"skill_key": skill_key, "name": entry.name, "skill_md_path": entry.skill_md_path, "mtime": mtime}
        rows.append(row)
        hasher.update(f"{skill_key}|{entry.skill_md_path}|{mtime}".encode("utf-8"))

    return {
        "version": hasher.hexdigest(),
        "count": len(rows),
        "skills": rows,
        "updated_at": _utc_now(),
    }


def refresh_snapshot(entries: list[SkillEntry]) -> dict[str, Any]:
    payload = build_snapshot(entries)
    get_snapshot_file().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload
