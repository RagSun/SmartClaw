"""长期记忆双层化测试（团队管沉淀 + 用户管个人偏好/归属，纯增量、默认开）。

覆盖：默认开首轮≈历史、团队/个人标签、个人层按用户隔离、冲突裁决规则、
compact 不丢层、晋升按 note_kind 分流、每层上限截断、删除用户记忆、关开关回落单层。
全部本地 SQLite + 临时目录，无网络。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from smartclaw.memory.context_helpers import compact_prefix_from_memory_context
from smartclaw.memory.manager import MemoryManager
from smartclaw.memory.session_maintainer import (
    _is_personal_note_kind,
    promote_notes_to_longterm_md,
)


def _mgr(tmpdir: str, user_id: str = "ou_a") -> MemoryManager:
    return MemoryManager(
        agent_id="bot",
        session_id="s1",
        channel="feishu",
        user_id=user_id,
        data_dir=Path(tmpdir),
    )


def _content_blocks(ctx: list[dict]) -> str:
    return "\n".join((c.get("content") or "") for c in ctx)


# ----------------------------- 默认开 = 首轮≈历史 ----------------------------- #
def test_default_on_first_turn_no_injection():
    """默认开但两层皆为模板（<400 字）→ 不注入，行为与历史一致。"""
    with tempfile.TemporaryDirectory() as d:
        m = _mgr(d)
        assert m._enable_user_longterm is True  # 默认开
        assert m.get_context_for_llm() == []
        m.close()


# ----------------------------- 标签：团队 vs 个人 ----------------------------- #
def test_team_layer_uses_team_label():
    with tempfile.TemporaryDirectory() as d:
        m = _mgr(d)
        m._longterm_memory.add_important_note("额定节拍60件每分钟 TEAM_FACT", note_kind="milestone")
        ctx = m.get_context_for_llm()
        blob = _content_blocks(ctx)
        assert "[团队知识]" in blob
        assert "TEAM_FACT" in blob
        assert "[用户记忆]" not in blob  # 旧误导标签已弃用
        m.close()


def test_personal_layer_uses_my_label_and_conflict_rule():
    with tempfile.TemporaryDirectory() as d:
        m = _mgr(d, user_id="ou_a")
        m.user_longterm_for("ou_a").add_learning("喜欢用表格汇报 PERSONAL_PREF", category="preference_draft")
        ctx = m.get_context_for_llm()
        blob = _content_blocks(ctx)
        assert "[我的记忆]" in blob
        assert "PERSONAL_PREF" in blob
        # 冲突裁决规则必须随个人层注入
        assert "记忆优先级" in blob
        assert "事实/合规以团队为准" in blob
        m.close()


# ----------------------------- 个人层按用户隔离（核心） ----------------------------- #
def test_personal_layer_isolated_between_users():
    with tempfile.TemporaryDirectory() as d:
        m = _mgr(d, user_id="ou_a")
        m.user_longterm_for("ou_a").add_learning("甲的私密偏好 SECRET_A", category="preference_draft")
        m.user_longterm_for("ou_b").add_learning("乙的私密偏好 SECRET_B", category="preference_draft")

        # 当前发言人=甲：看得到 A、看不到 B
        m.user_id = "ou_a"
        blob_a = _content_blocks(m.get_context_for_llm())
        assert "SECRET_A" in blob_a
        assert "SECRET_B" not in blob_a

        # 切到乙：看得到 B、看不到 A（杜绝跨用户广播）
        m.user_id = "ou_b"
        blob_b = _content_blocks(m.get_context_for_llm())
        assert "SECRET_B" in blob_b
        assert "SECRET_A" not in blob_b
        m.close()


def test_team_layer_shared_across_users():
    """团队层对所有用户共享（知识沉淀不丢）。"""
    with tempfile.TemporaryDirectory() as d:
        m = _mgr(d, user_id="ou_a")
        m._longterm_memory.add_important_note("2号线SOP TEAM_SHARED", note_kind="milestone")
        for uid in ("ou_a", "ou_b", "ou_c"):
            m.user_id = uid
            assert "TEAM_SHARED" in _content_blocks(m.get_context_for_llm())
        m.close()


# ----------------------------- compact 不丢层（埋雷点回归） ----------------------------- #
def test_compact_keeps_both_longterm_layers():
    mc = [
        {"role": "system", "content": "[团队知识] team…"},
        {"role": "system", "content": "[我的记忆] mine…"},
        {"role": "system", "content": "[对话摘要] sum…"},
        {"role": "user", "content": "noop"},
    ]
    kept = compact_prefix_from_memory_context(mc)
    contents = [c["content"] for c in kept]
    assert any(c.startswith("[团队知识]") for c in contents)
    assert any(c.startswith("[我的记忆]") for c in contents)
    assert any(c.startswith("[对话摘要]") for c in contents)
    assert len(kept) == 3  # user 行被排除


# ----------------------------- 晋升路由：偏好→个人，事实→团队 ----------------------------- #
def test_note_kind_classifier():
    assert _is_personal_note_kind("用户偏好") is True
    assert _is_personal_note_kind("用户禁止") is True
    assert _is_personal_note_kind("preference") is True
    assert _is_personal_note_kind("milestone") is False
    assert _is_personal_note_kind("历史问题") is False


def test_promotion_routes_preference_to_personal_fact_to_team():
    with tempfile.TemporaryDirectory() as d:
        m = _mgr(d, user_id="ou_a")
        # 高重要性（>=9 走硬层 add_important_note）
        m._store.add_memory_note(
            note_kind="用户偏好", content="喜欢深色模式 PREF_PERSONAL",
            importance=9, user_id="ou_a", agent_id="bot", tenant_id="default",
        )
        m._store.add_memory_note(
            note_kind="milestone", content="额定节拍60 FACT_TEAM",
            importance=9, user_id="ou_a", agent_id="bot", tenant_id="default",
        )
        n = promote_notes_to_longterm_md(m, user_id="ou_a")
        assert n == 2

        team = m._longterm_memory.get_content()
        personal = m.user_longterm_for("ou_a").get_content()
        # 偏好只进个人层、不污染团队层
        assert "PREF_PERSONAL" in personal
        assert "PREF_PERSONAL" not in team
        # 事实只进团队层
        assert "FACT_TEAM" in team
        assert "FACT_TEAM" not in personal
        m.close()


def test_promotion_falls_back_to_team_when_user_layer_disabled():
    """关掉用户级长期记忆 → 一切回落团队层（=历史单层行为，零回归）。"""
    with tempfile.TemporaryDirectory() as d:
        m = _mgr(d, user_id="ou_a")
        m._enable_user_longterm = False  # 模拟开关关闭
        m._store.add_memory_note(
            note_kind="用户偏好", content="偏好X PREF_BACK",
            importance=9, user_id="ou_a", agent_id="bot", tenant_id="default",
        )
        n = promote_notes_to_longterm_md(m, user_id="ou_a")
        assert n == 1
        assert "PREF_BACK" in m._longterm_memory.get_content()  # 落团队层
        # 个人层文件不应被创建
        assert not m.user_longterm_file("ou_a").exists()
        m.close()


# ----------------------------- 每层上限截断 ----------------------------- #
def test_per_layer_truncation_caps_personal():
    with tempfile.TemporaryDirectory() as d:
        m = _mgr(d, user_id="ou_a")
        m._user_lt_max_chars = 60  # 收紧个人层上限
        m.user_longterm_for("ou_a").add_learning("X" * 2000, category="preference_draft")
        blob = _content_blocks(m.get_context_for_llm())
        assert "[我的记忆]" in blob
        assert "已按上限截断" in blob
        m.close()


# ----------------------------- 合规：删除某用户全部个人记忆 ----------------------------- #
def test_delete_user_longterm():
    with tempfile.TemporaryDirectory() as d:
        m = _mgr(d, user_id="ou_a")
        m.user_longterm_for("ou_a").add_learning("待删 TO_DELETE", category="preference_draft")
        assert m.user_longterm_file("ou_a").exists()

        assert m.delete_user_longterm("ou_a") is True
        assert not m.user_longterm_file("ou_a").exists()
        # 缓存已清，重新解析得到全新模板（不含旧内容）
        m.user_id = "ou_a"
        assert "TO_DELETE" not in _content_blocks(m.get_context_for_llm())
        m.close()


def test_user_longterm_files_are_distinct_per_user():
    """个人层文件按 open_id 分目录 → 群聊按发言人取层的物理保证。"""
    with tempfile.TemporaryDirectory() as d:
        m = _mgr(d, user_id="ou_a")
        fa = m.user_longterm_file("ou_a")
        fb = m.user_longterm_file("ou_b")
        assert fa != fb
        assert "ou_a" in str(fa) and "ou_b" in str(fb)
        m.close()
