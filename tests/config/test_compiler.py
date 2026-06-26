"""
配置编译器测试（Markdown 位于执行工作区目录）
"""

import json
import tempfile
from pathlib import Path

import pytest

from smartclaw.config.compiler import ConfigCompiler


@pytest.fixture
def sample_agents_dir():
    """创建示例 agents 目录（agent.json 在 data/agents；Markdown 在执行 workspace）"""
    with tempfile.TemporaryDirectory() as tmpdir:
        agents_dir = Path(tmpdir)
        agent_dir = agents_dir / "test-agent"
        agent_dir.mkdir()

        ws = agents_dir / "test-agent-ws"
        ws.mkdir()

        base_config = {
            "name": "test-agent",
            "description": "测试 Agent",
            "channel": "feishu",
            "enabled": True,
            "workspace": str(ws.resolve()),
            "llm": {"provider": "glm", "model_name": "glm-4-flash"},
        }

        (agent_dir / "agent.json").write_text(
            json.dumps(base_config, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        soul_content = """# SOUL.md

## 核心定位

测试 Agent 核心定位
"""
        (ws / "SOUL.md").write_text(soul_content, encoding="utf-8")

        identity_content = """# IDENTITY.md

**姓名**：Test Agent
"""
        (ws / "IDENTITY.md").write_text(identity_content, encoding="utf-8")

        yield agents_dir


@pytest.mark.asyncio
async def test_compile_agent(sample_agents_dir):
    """测试编译单个 agent"""
    compiler = ConfigCompiler(sample_agents_dir)
    success = await compiler.compile_agent("test-agent")

    assert success

    compiled_file = (
        sample_agents_dir / "test-agent" / ".compiled" / "agent.compiled.json"
    )
    assert compiled_file.exists()

    compiled_config = json.loads(compiled_file.read_text(encoding="utf-8"))
    assert "metadata" in compiled_config
    assert "soul" in compiled_config
    assert "identity" in compiled_config
    assert "system_prompt" in compiled_config


@pytest.mark.asyncio
async def test_compile_all(sample_agents_dir):
    """测试编译所有 agent"""
    compiler = ConfigCompiler(sample_agents_dir)
    results = await compiler.compile_all()

    assert "test-agent" in results
    assert results["test-agent"]


@pytest.mark.asyncio
async def test_needs_recompile(sample_agents_dir):
    """测试重新编译检测"""
    compiler = ConfigCompiler(sample_agents_dir)
    agent_data = sample_agents_dir / "test-agent"
    ws = sample_agents_dir / "test-agent-ws"

    assert compiler._needs_recompile(agent_data)

    await compiler.compile_agent("test-agent")

    assert not compiler._needs_recompile(agent_data)

    soul_file = ws / "SOUL.md"
    soul_file.write_text("# Modified", encoding="utf-8")
    assert compiler._needs_recompile(agent_data)


def test_generate_system_prompt(sample_agents_dir):
    """测试系统提示生成"""
    compiler = ConfigCompiler(sample_agents_dir)
    ws = sample_agents_dir / "test-agent-ws"

    markdown_config = {
        "identity": {"name": "Test Agent", "creature": "AI", "atmosphere": "友好"},
        "soul": {
            "core_positioning": "测试定位",
            "core_capabilities": [{"category": "对话", "description": "回答问题"}],
        },
    }

    prompt = compiler._generate_system_prompt(markdown_config, ws)

    assert "Test Agent" in prompt
    assert "测试定位" in prompt
    assert "AI" in prompt


def test_file_hash(sample_agents_dir):
    """测试文件哈希计算"""
    compiler = ConfigCompiler(sample_agents_dir)
    agent_dir = sample_agents_dir / "test-agent"
    ws = sample_agents_dir / "test-agent-ws"

    hashes1 = compiler._calculate_hashes(agent_dir, ws)
    assert "agent.json" in hashes1
    assert "ws/SOUL.md" in hashes1

    soul_file = ws / "SOUL.md"
    soul_file.write_text("# Modified", encoding="utf-8")

    hashes2 = compiler._calculate_hashes(agent_dir, ws)
    assert hashes1["ws/SOUL.md"] != hashes2["ws/SOUL.md"]
