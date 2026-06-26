"""Agent 执行工作区解析与脚手架。"""
from smartclaw.agent.workspace import (
    merge_workspace_resolution_snap,
    resolve_agent_workspace_dir,
    scaffold_agent_workspace,
)
from smartclaw.config.loader import AppConfig, Config


def test_resolve_default_layout(monkeypatch, tmp_path):
    monkeypatch.setenv("SMARTCLAW_AGENT_WORKSPACE_BASE", str(tmp_path / "w"))
    p = resolve_agent_workspace_dir("bot1", {}, None)
    assert p == (tmp_path / "w" / "bot1").resolve()


def test_resolve_from_config_base(tmp_path):
    cfg = Config(smartclaw=AppConfig(agent_workspace_base=str(tmp_path / "w2")))
    p = resolve_agent_workspace_dir("bot1", {}, cfg)
    assert p == (tmp_path / "w2" / "bot1").resolve()


def test_resolve_absolute_override(tmp_path):
    custom = tmp_path / "custom"
    p = resolve_agent_workspace_dir("bot1", {"workspace": str(custom)}, None)
    assert p == custom.resolve()


def test_resolve_relative_to_base(monkeypatch, tmp_path):
    base = tmp_path / "w"
    monkeypatch.setenv("SMARTCLAW_AGENT_WORKSPACE_BASE", str(base))
    p = resolve_agent_workspace_dir("bot1", {"workspace": "sub"}, None)
    assert p == (base / "sub").resolve()


def test_merge_workspace_snap_profile_empty_workspace_falls_back_to_disk(tmp_path):
    full = {"name": "bot1", "workspace": "keep_me", "tenant_id": "default"}
    profile = {"workspace": ""}
    snap = merge_workspace_resolution_snap(full, profile)
    assert snap["workspace"] == "keep_me"


def test_merge_workspace_snap_profile_omitted_workspace_keeps_disk(tmp_path):
    full = {"name": "bot1", "workspace": "sub", "tenant_id": "default"}
    profile = {"display_name": "x"}
    snap = merge_workspace_resolution_snap(full, profile)
    assert snap["workspace"] == "sub"


def test_merge_workspace_snap_profile_overrides_workspace_key(tmp_path):
    full = {"name": "bot1", "workspace": "disk_sub", "tenant_id": "default"}
    profile = {"workspace": "from_profile"}
    snap = merge_workspace_resolution_snap(full, profile)
    assert snap["workspace"] == "from_profile"
    cfg = Config(smartclaw=AppConfig(agent_workspace_base=str(tmp_path)))
    logical = str(snap.get("name") or "")
    resolved = resolve_agent_workspace_dir(logical, snap, cfg, tenant_id="default")
    assert resolved == (tmp_path / "from_profile").resolve()


def test_scaffold_creates_md(tmp_path):
    created = scaffold_agent_workspace(tmp_path, skip_existing=True)
    assert "AGENTS.md" in created
    assert "skills/README.md" in created
    assert "tools/README.md" in created
    assert (tmp_path / "AGENTS.md").is_file()
    assert (tmp_path / "skills" / "README.md").is_file()
    assert (tmp_path / "tools" / "README.md").is_file()


def test_scaffold_skip_existing(tmp_path):
    scaffold_agent_workspace(tmp_path, skip_existing=True)
    second = scaffold_agent_workspace(tmp_path, skip_existing=True)
    assert second == []
