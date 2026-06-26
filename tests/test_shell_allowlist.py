"""exec Shell 白名单合并逻辑。"""

from pathlib import Path

from smartclaw.agent.shell_allowlist import evaluate_shell_allowlist, is_shell_command_allowed
from smartclaw.config.loader import Config, ExecutionConfig


def test_empty_patterns_allow(tmp_path):
    cfg = Config(execution=ExecutionConfig())
    ok, _ = is_shell_command_allowed(
        "rm -rf /",
        cfg=cfg,
        agent_config={},
        workspace_root=tmp_path,
    )
    assert ok


def test_global_allowlist(tmp_path):
    cfg = Config(execution=ExecutionConfig(shell_allowlist=["python", "pytest"]))
    ok, _ = is_shell_command_allowed("python -V", cfg=cfg, agent_config={}, workspace_root=tmp_path)
    assert ok
    ok2, msg = is_shell_command_allowed("ruby -v", cfg=cfg, agent_config={}, workspace_root=tmp_path)
    assert not ok2
    assert "白名单" in msg


def test_workspace_file_merge(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "SHELL_ALLOWLIST.txt").write_text("git\n", encoding="utf-8")
    cfg = Config(execution=ExecutionConfig())
    ok, _ = is_shell_command_allowed("git status", cfg=cfg, agent_config={}, workspace_root=tmp_path)
    assert ok
    ok2, _ = is_shell_command_allowed("curl x", cfg=cfg, agent_config={}, workspace_root=tmp_path)
    assert not ok2


def test_agent_json_list(tmp_path):
    cfg = Config(execution=ExecutionConfig())
    ac = {"shell_allowlist": ["echo"]}
    ok, _ = is_shell_command_allowed("echo hi", cfg=cfg, agent_config=ac, workspace_root=tmp_path)
    assert ok


def test_star_sentinel_allows_arbitrary_command(tmp_path):
    """单独 *：Shell 层放行；高危仍应由 Tool Policy 拦截（本单测仅覆盖 Shell 层）。"""
    cfg = Config(execution=ExecutionConfig(shell_allowlist=["*"]))
    ok, _ = is_shell_command_allowed(
        "some-random-cli --flag",
        cfg=cfg,
        agent_config={},
        workspace_root=tmp_path,
    )
    assert ok


def test_fnmatch_pattern_on_command(tmp_path):
    cfg = Config(execution=ExecutionConfig())
    ac = {"shell_allowlist": ["python*", "node*"]}
    ok, _ = is_shell_command_allowed(
        "python -m streamlit run app.py",
        cfg=cfg,
        agent_config=ac,
        workspace_root=tmp_path,
    )
    assert ok
    ok2, _ = is_shell_command_allowed("node --version", cfg=cfg, agent_config=ac, workspace_root=tmp_path)
    assert ok2
    ok3, msg = is_shell_command_allowed("ruby -v", cfg=cfg, agent_config=ac, workspace_root=tmp_path)
    assert not ok3
    assert "首词=" in msg


def test_double_star_same_as_star(tmp_path):
    cfg = Config(execution=ExecutionConfig(shell_allowlist=["**"]))
    ok, _ = is_shell_command_allowed("anything", cfg=cfg, agent_config={}, workspace_root=tmp_path)
    assert ok


def test_evaluate_shell_allowlist_matched_pattern(tmp_path):
    cfg = Config(execution=ExecutionConfig(shell_allowlist=["python", "git"]))
    ev = evaluate_shell_allowlist(
        "python -m uv pip install x",
        cfg=cfg,
        agent_config={},
        workspace_root=tmp_path,
    )
    assert ev.allowed
    assert ev.matched_pattern == "python"
    assert ev.first_token == "python"
    assert ev.pattern_count == 2
    assert not ev.merge_contains_unrestricted_sentinel
    assert "python" in ev.patterns_preview


def test_evaluate_shell_allowlist_deny_includes_preview(tmp_path):
    cfg = Config(execution=ExecutionConfig(shell_allowlist=["echo"]))
    ev = evaluate_shell_allowlist(
        "curl https://x",
        cfg=cfg,
        agent_config={},
        workspace_root=tmp_path,
    )
    assert not ev.allowed
    assert ev.matched_pattern is None
    assert ev.first_token == "curl"
    assert "echo" in ev.patterns_preview


def test_evaluate_shell_allowlist_empty_merge(tmp_path):
    cfg = Config(execution=ExecutionConfig())
    ev = evaluate_shell_allowlist(
        "anything-goes",
        cfg=cfg,
        agent_config={},
        workspace_root=tmp_path,
    )
    assert ev.allowed
    assert ev.pattern_count == 0
    assert ev.first_token == "anything-goes"
