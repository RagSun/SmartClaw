"""Agent 创建的渠道分支测试：飞书 per-agent 凭证 vs 企业微信全局凭证。

覆盖 ``AgentManager.create_agent`` 的渠道分支，以及 ``agent add`` CLI 的渠道校验。
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

import smartclaw.paths as paths
from smartclaw.agent.manager import AgentManager, CreateAgentRequest
from smartclaw.cli import agent_app


@pytest.fixture
def isolated_agents(monkeypatch, tmp_path):
    """把 Agent 配置目录、工作区脚手架、全局 config 隔离到 tmp，避免触碰真实安装根。"""
    agents_root = tmp_path / "agents"
    agents_root.mkdir()
    monkeypatch.setattr(paths, "AGENTS_DIR", agents_root)
    monkeypatch.setattr(paths, "USER_AGENTS_DIR", agents_root)
    monkeypatch.setattr(paths, "get_agents_dirs", lambda: [agents_root])

    # create_agent 成功后会同步工作区脚手架并读取全局 config —— 测试中均打桩隔离
    import smartclaw.agent.workspace as workspace

    monkeypatch.setattr(workspace, "resolve_agent_workspace_dir", lambda *a, **k: tmp_path / "ws")
    monkeypatch.setattr(workspace, "scaffold_agent_workspace", lambda *a, **k: [])

    import smartclaw.config.loader as loader

    monkeypatch.setattr(loader, "get_config", lambda: None)
    return agents_root


def _read_agent(manager: AgentManager, name: str) -> dict:
    return manager._read_config(name) or {}


def test_create_feishu_agent_writes_per_agent_credentials(isolated_agents) -> None:
    manager = AgentManager()
    req = CreateAgentRequest(
        name="myfeishu",
        display_name="",  # 触发渠道默认命名
        channel="feishu",
        app_id="cli_test123",
        app_secret="supersecretkey12345",
    )
    ok, msg, _info = manager.create_agent(req)
    assert ok, msg

    data = _read_agent(manager, "myfeishu")
    assert data["channel"] == "feishu"
    assert data["feishu"]["app_id"] == "cli_test123"
    assert data["feishu"]["app_secret"].startswith("ENC:")
    assert data["display_name"] == "SmartClaw-myfeishu"
    assert data["aliases"] == ["SmartClaw-myfeishu"]


def test_create_wecom_agent_has_no_feishu_block(isolated_agents) -> None:
    manager = AgentManager()
    req = CreateAgentRequest(
        name="mywecom",
        display_name="",
        channel="wecom",
        # 企业微信为全局单 App，无需 per-agent 凭证
    )
    ok, msg, _info = manager.create_agent(req)
    assert ok, msg

    data = _read_agent(manager, "mywecom")
    assert data["channel"] == "wecom"
    assert "feishu" not in data
    assert data["display_name"] == "mywecom"
    assert data["aliases"] == ["mywecom"]


def test_create_wecom_ignores_app_id_argument(isolated_agents) -> None:
    manager = AgentManager()
    req = CreateAgentRequest(
        name="mywecom2",
        display_name="",
        channel="wecom",
        app_id="cli_should_be_ignored",
        app_secret="ignoredsecret12345",
    )
    ok, msg, _info = manager.create_agent(req)
    assert ok, msg

    data = _read_agent(manager, "mywecom2")
    assert data["channel"] == "wecom"
    assert "feishu" not in data  # app_id/app_secret 被忽略，不写 feishu 块


def test_create_feishu_rejects_missing_app_id(isolated_agents) -> None:
    manager = AgentManager()
    req = CreateAgentRequest(
        name="badfeishu",
        display_name="Bad",
        channel="feishu",
        app_id="",
        app_secret="",
    )
    ok, msg, _info = manager.create_agent(req)
    assert not ok
    assert "AppID" in msg


def test_create_feishu_rejects_bad_app_id_format(isolated_agents) -> None:
    manager = AgentManager()
    req = CreateAgentRequest(
        name="badfmt",
        display_name="Bad",
        channel="feishu",
        app_id="not_cli_format",
        app_secret="supersecretkey12345",
    )
    ok, msg, _info = manager.create_agent(req)
    assert not ok
    assert "cli_" in msg


def test_agent_add_cli_rejects_invalid_channel() -> None:
    result = CliRunner().invoke(agent_app, ["add", "bad", "--channel", "telegram"])
    assert result.exit_code == 1


def test_agent_add_cli_rejects_feishu_missing_credentials() -> None:
    result = CliRunner().invoke(agent_app, ["add", "bad", "--channel", "feishu"])
    assert result.exit_code == 1
