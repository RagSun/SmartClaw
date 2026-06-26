"""bg_probe_decl：声明合并。"""

from __future__ import annotations

import json
from pathlib import Path

from smartclaw.agent.bg_probe_decl import resolve_bg_probe_decl


def test_workspace_overrides_agent(tmp_path):
    agent = {"execution": {"bg_probe": {"after_smoke": "echo OLD", "tcp_probe": False}}}
    (tmp_path / ".smartclaw").mkdir(parents=True, exist_ok=True)
    probe_path = tmp_path / ".smartclaw" / "bg_probe.json"
    probe_path.write_text(
        json.dumps(
            {"after_smoke": "echo NEW", "tcp_probe": True, "after_smoke_timeout_sec": 5}
        ),
        encoding="utf-8",
    )
    decl = resolve_bg_probe_decl(agent_cfg=agent, workspace_root=tmp_path)
    assert decl.after_smoke.strip() == "echo NEW"
    assert decl.tcp_probe is True
    assert decl.after_smoke_timeout_sec == 5


def test_top_level_bg_probe_fallback(tmp_path):
    agent = {"bg_probe": {"after_smoke": "true", "tcp_probe": False}}
    decl = resolve_bg_probe_decl(agent_cfg=agent, workspace_root=tmp_path)
    assert "true" in decl.after_smoke
    assert decl.tcp_probe is False
