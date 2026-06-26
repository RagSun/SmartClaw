"""宿主命令门：Tool Policy + Shell 白名单统一判定。"""

from pathlib import Path

from smartclaw.agent.host_command_gate import evaluate_host_command
from smartclaw.config.loader import Config, ExecutionConfig


def test_empty_allowlist_policy_only(tmp_path):
    cfg = Config(execution=ExecutionConfig())
    v = evaluate_host_command(
        "python -c 'print(1)'",
        cfg=cfg,
        agent_config={},
        workspace_root=tmp_path,
    )
    assert v.allowed
    assert v.rule_id == "host_command:ok"
    assert v.layer == "host_command"


def test_shell_allowlist_deny_rule_id(tmp_path):
    cfg = Config(execution=ExecutionConfig(shell_allowlist=["python"]))
    v = evaluate_host_command(
        "ruby -v",
        cfg=cfg,
        agent_config={},
        workspace_root=tmp_path,
    )
    assert not v.allowed
    assert v.layer == "shell_allowlist"
    assert v.rule_id == "shell_allowlist:no_match"


def test_tool_policy_deny_rule_id(tmp_path):
    cfg = Config(execution=ExecutionConfig())
    v = evaluate_host_command(
        "rm -rf /",
        cfg=cfg,
        agent_config={},
        workspace_root=tmp_path,
    )
    assert not v.allowed
    assert v.layer == "tool_policy"
    assert v.rule_id == "tool_policy:deny"


def test_tool_policy_elevated_rule_id(tmp_path):
    cfg = Config(execution=ExecutionConfig())
    v = evaluate_host_command(
        "docker ps",
        cfg=cfg,
        agent_config={},
        workspace_root=tmp_path,
    )
    assert not v.allowed
    assert v.layer == "tool_policy"
    assert v.rule_id == "tool_policy:elevated"
