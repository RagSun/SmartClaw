"""
Run skill smoke tests.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from smartclaw.skills.loader import load_workspace_skill_entries
from smartclaw.subprocess_io import SUBPROCESS_TEXT_KWARGS


def run_workspace_skill_tests(workspace_dir: str, config: Any = None) -> dict[str, Any]:
    entries = load_workspace_skill_entries(workspace_dir, config=config)
    results: list[dict[str, Any]] = []
    ok = True
    for entry in entries:
        test_file = Path(entry.base_dir) / "tests" / "smoke.py"
        if not test_file.exists():
            results.append(
                {
                    "skill_key": entry.metadata.skill_key or entry.name,
                    "ok": False,
                    "reason": "missing tests/smoke.py",
                }
            )
            ok = False
            continue
        proc = subprocess.run(
            ["python", str(test_file)],
            cwd=entry.base_dir,
            capture_output=True,
            text=True,
            **SUBPROCESS_TEXT_KWARGS,
        )
        passed = proc.returncode == 0
        results.append(
            {
                "skill_key": entry.metadata.skill_key or entry.name,
                "ok": passed,
                "output": ((proc.stdout or "") + (proc.stderr or "")).strip(),
            }
        )
        if not passed:
            ok = False
    return {"workspace_dir": workspace_dir, "ok": ok, "total": len(results), "skills": results}
