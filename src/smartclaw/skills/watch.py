"""
Skill watcher based on snapshot version.
"""

from __future__ import annotations

import time
from typing import Any

from smartclaw.skills.loader import load_workspace_skill_entries
from smartclaw.skills.storage import build_snapshot, refresh_snapshot


def refresh_workspace_snapshot(workspace_dir: str, config: Any = None) -> dict[str, Any]:
    entries = load_workspace_skill_entries(workspace_dir, config=config)
    return refresh_snapshot(entries)


def watch_workspace_skills(workspace_dir: str, config: Any = None, interval_seconds: float = 1.0):
    current = build_snapshot(load_workspace_skill_entries(workspace_dir, config=config))
    refresh_snapshot(load_workspace_skill_entries(workspace_dir, config=config))
    while True:
        time.sleep(max(0.2, interval_seconds))
        nxt = build_snapshot(load_workspace_skill_entries(workspace_dir, config=config))
        if nxt["version"] != current["version"]:
            refresh_snapshot(load_workspace_skill_entries(workspace_dir, config=config))
            yield {
                "changed": True,
                "old_version": current["version"],
                "new_version": nxt["version"],
                "count": nxt["count"],
            }
            current = nxt
