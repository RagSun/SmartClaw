"""MemoryManager v2.0 完整测试套件"""

import tempfile
from pathlib import Path

from smartclaw.memory.manager import MemoryManager


def test_memory_manager_creation():
    """测试创建"""
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = MemoryManager(
            agent_id="test",
            session_id="session1",
            channel="feishu",
            user_id="user1",
            data_dir=Path(tmpdir),
        )
        assert manager.agent_id == "test"
        assert manager._store is not None
        assert manager._auto_summary is not None
        manager.close()


def test_add_message():
    """测试添加消息"""
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = MemoryManager(
            agent_id="test",
            session_id="session1",
            channel="feishu",
            user_id="user1",
            data_dir=Path(tmpdir),
        )

        manager.add_message("user", "Hello")
        manager.add_message("assistant", "Hi")

        # v2.0: 通过 store 查询
        count = manager._store.get_message_count("session1")
        assert count == 2
        manager.close()


def test_get_context_for_llm():
    """测试获取 LLM 上下文"""
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = MemoryManager(
            agent_id="test",
            session_id="session1",
            channel="feishu",
            user_id="user1",
            data_dir=Path(tmpdir),
        )

        manager.add_message("user", "What's your name?")
        manager.add_message("assistant", "I am test")

        # v2.0: 返回 list[dict]；显式拉取 SQLite transcript（默认 SSOT 为 Session，不附带逐条）
        context = manager.get_context_for_llm(include_stored_transcript=True)

        assert isinstance(context, list)
        assert len(context) == 2
        assert context[0]["role"] == "user"
        assert context[1]["role"] == "assistant"
        manager.close()


def test_get_context_for_llm_default_no_sqlite_transcript():
    """默认不包含 SQLite 逐条，仅摘要/长记忆（与 Runner 的 Session SSOT 一致）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = MemoryManager(
            agent_id="test",
            session_id="session1",
            channel="feishu",
            user_id="user1",
            data_dir=Path(tmpdir),
        )

        manager.add_message("user", "Ping")
        manager.add_message("assistant", "Pong")

        ctx = manager.get_context_for_llm()
        assert ctx == []

        manager.close()


def test_extract_important_note():
    """测试提取重要记忆要点"""
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = MemoryManager(
            agent_id="test",
            session_id="session1",
            channel="feishu",
            user_id="user1",
            data_dir=Path(tmpdir),
        )

        manager.extract_important_note(
            "User prefers Chinese",
            note_kind="preference",
            importance=8,
        )

        # v2.0: 通过 store 查询
        notes = manager._store.get_memory_notes(user_id="user1")
        assert len(notes) >= 1
        assert any("Chinese" in e["content"] for e in notes)
        manager.close()


def test_auto_extract_memory_notes():
    """测试自动提取记忆要点"""
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = MemoryManager(
            agent_id="test",
            session_id="session1",
            channel="feishu",
            user_id="user1",
            data_dir=Path(tmpdir),
        )

        manager.add_message("user", "记住我喜欢用中文交流")
        manager.add_message("assistant", "好的")

        count = manager.auto_extract_memory_notes()
        assert count >= 1
        manager.close()


def test_auto_extract_memory_notes_dedupes_identical_rows():
    """同一 user/agent/kind/content 重复抽取时不重复插入 memory_notes。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = MemoryManager(
            agent_id="test",
            session_id="session1",
            channel="feishu",
            user_id="user1",
            data_dir=Path(tmpdir),
        )
        manager.add_message("user", "记住我喜欢深色模式")
        manager.add_message("assistant", "好的")

        first = manager.auto_extract_memory_notes()
        assert first >= 1
        notes_after_first = manager._store.get_memory_notes(
            user_id="user1", agent_id="test", limit=100
        )
        n_after_first = len(notes_after_first)

        second = manager.auto_extract_memory_notes()
        assert second == 0
        notes_after_second = manager._store.get_memory_notes(
            user_id="user1", agent_id="test", limit=100
        )
        assert len(notes_after_second) == n_after_first
        manager.close()


def test_auto_extract_memory_notes_explicit_session():
    """显式 session_id，避免依赖共享 manager 上的 session_id（后台任务场景）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = MemoryManager(
            agent_id="test",
            session_id="other",
            channel="feishu",
            user_id="user1",
            data_dir=Path(tmpdir),
        )
        manager.session_id = "other"
        manager.add_message("user", "记住我喜欢深色模式")

        manager.session_id = ""
        n = manager.auto_extract_memory_notes(session_id="other", tenant_id="default", user_id="user1")
        assert n >= 1
        manager.close()


def test_create_summary():
    """测试创建摘要"""
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = MemoryManager(
            agent_id="test",
            session_id="session1",
            channel="feishu",
            user_id="user1",
            data_dir=Path(tmpdir),
        )

        manager.add_message("user", "Hello")
        manager.add_message("assistant", "Hi")

        manager.create_summary("这是一个测试会话")

        summary = manager._store.get_latest_summary("session1")
        assert summary is not None
        assert "测试会话" in summary["summary"]
        manager.close()


def test_create_summary_explicit_session_no_shared_state():
    """显式 session_id/tenant_id 写入摘要（后台任务不依赖 manager.session_id）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = MemoryManager(
            agent_id="test",
            session_id="",
            channel="feishu",
            user_id="user1",
            data_dir=Path(tmpdir),
        )
        manager._store.add_message("session_x", "user", "hi", tenant_id="default")

        manager.create_summary("仅显式 sid", session_id="session_x", tenant_id="default")
        s = manager._store.get_latest_summary("session_x", tenant_id="default")
        assert s is not None
        assert "显式" in s["summary"]
        manager.close()


def test_get_usage_report():
    """测试使用报告"""
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = MemoryManager(
            agent_id="test",
            session_id="session1",
            channel="feishu",
            user_id="user1",
            data_dir=Path(tmpdir),
        )

        manager.add_message("user", "Hello")

        report = manager.get_usage_report()

        assert "session_id" in report
        assert "message_count" in report
        assert report["message_count"] == 1
        manager.close()


def test_cleanup_expired():
    """测试清理过期数据"""
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = MemoryManager(
            agent_id="test",
            session_id="session1",
            channel="feishu",
            user_id="user1",
            data_dir=Path(tmpdir),
        )

        result = manager.cleanup_expired()

        assert "cleaned" in result
        manager.close()


def test_get_user_profile():
    """测试用户画像"""
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = MemoryManager(
            agent_id="test",
            session_id="session1",
            channel="feishu",
            user_id="user1",
            data_dir=Path(tmpdir),
        )

        manager.update_user_profile("language", "Chinese", confidence=8)

        profile = manager.get_user_profile()
        assert profile.get("language") == "Chinese"
        manager.close()
