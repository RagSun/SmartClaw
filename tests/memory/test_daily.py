"""
日常记忆测试
"""

import tempfile
from datetime import datetime
from pathlib import Path

from smartclaw.memory.daily import DailyMemory


def test_daily_memory_creation():
    """测试日常记忆创建"""
    with tempfile.TemporaryDirectory() as tmpdir:
        memory = DailyMemory(agent_id="test-agent", memory_dir=Path(tmpdir))

        assert memory.agent_id == "test-agent"
        assert memory.memory_dir.exists()


def test_add_daily_note():
    """测试添加记录"""
    with tempfile.TemporaryDirectory() as tmpdir:
        memory = DailyMemory(agent_id="test-agent", memory_dir=Path(tmpdir))

        memory.add_daily_note("测试事件", note_kind="important")

        # 验证文件已创建
        today = datetime.now().strftime("%Y-%m-%d")
        memory_file = Path(tmpdir) / f"{today}.md"

        assert memory_file.exists()

        content = memory_file.read_text(encoding="utf-8")
        assert "测试事件" in content
        assert "important" in content


def test_get_daily_notes():
    """测试获取记录"""
    with tempfile.TemporaryDirectory() as tmpdir:
        memory = DailyMemory(agent_id="test-agent", memory_dir=Path(tmpdir))

        memory.add_daily_note("事件1")
        memory.add_daily_note("事件2")

        notes = memory.get_daily_notes()

        assert "事件1" in notes
        assert "事件2" in notes


def test_get_recent_daily_notes():
    """测试获取最近记录"""
    with tempfile.TemporaryDirectory() as tmpdir:
        memory = DailyMemory(agent_id="test-agent", memory_dir=Path(tmpdir))

        # 添加今天的记录
        memory.add_daily_note("今天事件")

        recent = memory.get_recent_daily_notes(days=1)

        assert len(recent) > 0
        today = datetime.now().strftime("%Y-%m-%d")
        assert today in recent


def test_search_daily_notes():
    """测试搜索记录"""
    with tempfile.TemporaryDirectory() as tmpdir:
        memory = DailyMemory(agent_id="test-agent", memory_dir=Path(tmpdir))

        memory.add_daily_note("Python 开发")
        memory.add_daily_note("JavaScript 学习")

        results = memory.search_daily_notes("Python")

        assert len(results) > 0
        assert "Python 开发" in results[0]["content"]


def test_get_summary():
    """测试获取摘要"""
    with tempfile.TemporaryDirectory() as tmpdir:
        memory = DailyMemory(agent_id="test-agent", memory_dir=Path(tmpdir))

        memory.add_daily_note("重要事件1", note_kind="important")
        memory.add_daily_note("重要事件2", note_kind="decision")

        summary = memory.get_summary()

        assert "important" in summary or "事件" in summary or len(summary) > 0


def test_cleanup_expired():
    """测试清理过期记忆"""
    with tempfile.TemporaryDirectory() as tmpdir:
        memory = DailyMemory(
            agent_id="test-agent", retention_days=1, memory_dir=Path(tmpdir)
        )

        # 创建一个旧文件
        old_date = "2020-01-01"
        old_file = Path(tmpdir) / f"{old_date}.md"
        old_file.write_text("# 旧记忆\n", encoding="utf-8")

        # 添加今天的记录
        memory.add_daily_note("今天事件")

        # 清理
        cleaned = memory.cleanup_expired()

        assert cleaned == 1
        assert not old_file.exists()


def test_daily_note_with_metadata():
    """测试带元数据的记录"""
    with tempfile.TemporaryDirectory() as tmpdir:
        memory = DailyMemory(agent_id="test-agent", memory_dir=Path(tmpdir))

        metadata = {
            "项目": "SmartClaw",
            "版本": "v1.0",
            "作者": "DT",
        }

        memory.add_daily_note("重要决策", note_kind="decision", metadata=metadata)

        notes = memory.get_daily_notes()

        assert "SmartClaw" in notes
        assert "v1.0" in notes
