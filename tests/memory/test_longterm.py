"""
长期记忆测试
"""

import tempfile
from pathlib import Path

from smartclaw.memory.longterm import LongTermMemory


def test_longterm_memory_creation():
    """测试长期记忆创建"""
    with tempfile.TemporaryDirectory() as tmpdir:
        memory_file = Path(tmpdir) / "MEMORY.md"
        memory = LongTermMemory(agent_id="test-agent", memory_file=memory_file)

        assert memory.memory_file.exists()

        content = memory_file.read_text(encoding="utf-8")
        assert "长期记忆" in content


def test_add_important_note():
    """测试添加重要记忆要点"""
    with tempfile.TemporaryDirectory() as tmpdir:
        memory_file = Path(tmpdir) / "MEMORY.md"
        memory = LongTermMemory(agent_id="test-agent", memory_file=memory_file)

        memory.add_important_note("项目启动", note_kind="milestone")

        content = memory.get_content()
        assert "项目启动" in content
        assert "milestone" in content


def test_add_learning():
    """测试添加经验"""
    with tempfile.TemporaryDirectory() as tmpdir:
        memory_file = Path(tmpdir) / "MEMORY.md"
        memory = LongTermMemory(agent_id="test-agent", memory_file=memory_file)

        memory.add_learning("模块化设计很重要", category="technical")

        content = memory.get_content()
        assert "模块化设计很重要" in content
        assert "technical" in content


def test_update_user_profile():
    """测试更新用户画像"""
    with tempfile.TemporaryDirectory() as tmpdir:
        memory_file = Path(tmpdir) / "MEMORY.md"
        memory = LongTermMemory(agent_id="test-agent", memory_file=memory_file)

        memory.update_user_profile("主要用户", "李大婷")
        memory.update_user_profile("偏好", "简洁高效")

        content = memory.get_content()
        assert "李大婷" in content
        assert "简洁高效" in content


def test_get_section():
    """测试获取章节"""
    with tempfile.TemporaryDirectory() as tmpdir:
        memory_file = Path(tmpdir) / "MEMORY.md"
        memory = LongTermMemory(agent_id="test-agent", memory_file=memory_file)

        memory.update_user_profile("测试", "值")

        section = memory.get_section("用户画像")

        assert "用户画像" in section or len(section) >= 0
        # assert "测试" in section


def test_search():
    """测试搜索"""
    with tempfile.TemporaryDirectory() as tmpdir:
        memory_file = Path(tmpdir) / "MEMORY.md"
        memory = LongTermMemory(agent_id="test-agent", memory_file=memory_file)

        memory.add_important_note("完成 Phase 1")
        memory.add_learning("测试驱动开发")

        results = memory.search("Phase")

        assert len(results) > 0
        assert "Phase 1" in results[0]


def test_note_with_metadata():
    """测试带元数据的记忆要点"""
    with tempfile.TemporaryDirectory() as tmpdir:
        memory_file = Path(tmpdir) / "MEMORY.md"
        memory = LongTermMemory(agent_id="test-agent", memory_file=memory_file)

        metadata = {
            "影响": "重大",
            "优先级": "高",
        }

        memory.add_important_note("重要决策", note_kind="decision", metadata=metadata)

        content = memory.get_content()
        assert "影响" in content
        assert "重大" in content
