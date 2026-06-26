"""
每轮对话后的记忆维护：会话摘要（LLM 写入 SQLite）、周期性事件抽取、高价值事件入 MEMORY.md。

与 DeepAgents 内置 SummarizationMiddleware 互补：此处将会话级摘要持久化，
供 get_context_for_llm 的 [对话摘要] 注入。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Optional

from smartclaw.console import error, info, warning
from smartclaw.llm.base import Message as LLMMessage
from smartclaw.llm.registry import get_llm_registry
from smartclaw.memory.hash_dedupe import append_hash, load_hash_set, promote_entry_hash
from smartclaw.memory.storage.auto_summary import AutoSummary
from smartclaw.monitoring.metrics import record_token_usage

if TYPE_CHECKING:
    from smartclaw.memory.manager import MemoryManager

# 首摘要：SQLite 中本会话消息条数达到该值后尝试生成
SUMMARY_FIRST_THRESHOLD = 50
# 之后每新增多少条消息再滚动摘要（避免每轮都打 LLM）
SUMMARY_REPEAT_MIN_NEW = 24
# 参与摘要的最近消息条数
SUMMARY_TRANSCRIPT_LIMIT = 100
# 单条内容截断，避免超长助手回复撑爆摘要请求
SUMMARY_MAX_CHARS_PER_MESSAGE = 2000
# 并入摘要 prompt 的旧摘要长度上限
PREVIOUS_SUMMARY_MAX_CHARS = 3500
# 每 N 条消息做一次规则事件抽取（降低成本）
AUTO_EXTRACT_EVERY_N_MESSAGES = 12
# 私聊 / 群聊：参与规则抽取的 SQLite 最近消息条数（群聊用小窗口对齐当前回合）
PRIVATE_CHAT_EXTRACT_MESSAGES = 100
GROUP_CHAT_EXTRACT_MESSAGES = 8
# 从 memory_notes 表晋升到 MEMORY.md：单轮最多个数、硬/软分层（硬=重要要点，软=学到的经验）
PROMOTE_TO_LONGTERM_MAX_PER_TURN = 3
PROMOTE_TO_LONGTERM_MIN_IMPORTANCE = 9  # 硬阈值：写入「重要事件」
PROMOTE_SOFT_MIN_IMPORTANCE = 7  # 软阈值：写入「学到的经验」作偏好草稿
PROMOTE_CONTENT_MAX_CHARS = 800
# 后台维护：摘要前静默等待，合并突发回合（秒）
MEMORY_MAINT_DEBOUNCE_SEC = 0.45

# 晋升路由：偏好/画像/用户专属类 → 个人层（不广播）；客观事实/里程碑类 → 团队层（共享沉淀）。
# 命中任一关键字即判为「个人」；其余归「团队」。与上下文注入的冲突裁决规则一致。
_PERSONAL_NOTE_KEYWORDS = (
    "偏好", "preference", "画像", "profile",
    "禁止", "喜欢", "习惯", "称呼", "用户",
)


def _is_personal_note_kind(note_kind: str) -> bool:
    nk = (note_kind or "").lower()
    return any(kw.lower() in nk for kw in _PERSONAL_NOTE_KEYWORDS)


def should_run_session_summary(
    msg_count: int,
    latest: Optional[dict[str, Any]],
    *,
    first_threshold: int = SUMMARY_FIRST_THRESHOLD,
    repeat_min_new: int = SUMMARY_REPEAT_MIN_NEW,
) -> bool:
    if msg_count < first_threshold:
        return False
    if not latest:
        return True
    prev = int(latest.get("original_count") or 0)
    return msg_count - prev >= repeat_min_new


def _format_transcript(messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role", "")
        if role not in ("user", "assistant"):
            continue
        text = (msg.get("content") or "").strip()
        if not text:
            continue
        if len(text) > SUMMARY_MAX_CHARS_PER_MESSAGE:
            text = text[:SUMMARY_MAX_CHARS_PER_MESSAGE] + "…"
        lines.append(f"[{role}]: {text}")
    return "\n".join(lines)


def promote_notes_to_longterm_md(
    memory_manager: MemoryManager,
    *,
    user_id: str,
    max_promote: int = PROMOTE_TO_LONGTERM_MAX_PER_TURN,
    hard_min_importance: int = PROMOTE_TO_LONGTERM_MIN_IMPORTANCE,
    soft_min_importance: int = PROMOTE_SOFT_MIN_IMPORTANCE,
) -> int:
    """
    将 SQLite memory_notes 晋升到 MEMORY.md：重要性≥hard_min 进「重要事件」；
    [soft_min, hard_min) 进「学到的经验」（偏好草稿）。硬层优先，再写软层。
    """
    notes = memory_manager._store.get_memory_notes(
        user_id=user_id,
        agent_id=memory_manager.agent_id,
        limit=40,
        tenant_id=memory_manager.tenant_id,
    )
    promoted = 0
    promoted_hashes = load_hash_set(memory_manager.promoted_hashes_file)
    # 个人层（按发言人 user_id 显式解析）：未开启用户级长期记忆 / 无 user_id 时为 None，
    # 此时所有晋升回落到团队层 = 历史单层行为，零回归。
    personal_lt = memory_manager.user_longterm_for(user_id)

    def try_promote(note: dict[str, Any], *, hard: bool) -> bool:
        nonlocal promoted_hashes
        content = (note.get("content") or "").strip()
        if len(content) < 4:
            return False
        text = content[:PROMOTE_CONTENT_MAX_CHARS]
        nk = str(note.get("note_kind") or "preference")
        imp = int(note.get("importance") or 0)
        ph = promote_entry_hash(
            user_id=user_id,
            agent_id=memory_manager.agent_id,
            note_kind=nk,
            content=text,
        )
        if ph in promoted_hashes:
            return False
        # 路由：个人偏好/画像类 → 个人层（不广播）；客观事实/里程碑类 → 团队层。
        target = (
            personal_lt
            if (personal_lt is not None and _is_personal_note_kind(nk))
            else memory_manager._longterm_memory
        )
        if hard:
            target.add_important_note(
                text,
                note_kind=nk,
                metadata={"source": "memory_notes", "importance": imp, "tier": "hard"},
            )
        else:
            target.add_learning(
                f"[偏好草稿 · {nk}] {text}",
                category="preference_draft",
            )
        append_hash(memory_manager.promoted_hashes_file, ph)
        promoted_hashes.add(ph)
        return True

    for note in notes:
        if promoted >= max_promote:
            break
        imp = int(note.get("importance") or 0)
        if imp < hard_min_importance:
            continue
        if try_promote(note, hard=True):
            promoted += 1

    for note in notes:
        if promoted >= max_promote:
            break
        imp = int(note.get("importance") or 0)
        if imp < soft_min_importance or imp >= hard_min_importance:
            continue
        if try_promote(note, hard=False):
            promoted += 1

    return promoted


async def maybe_refresh_session_summary_with_llm(
    *,
    memory_manager: MemoryManager,
    adapter_name: str,
    agent_id: str,
    session_id: str,
    tenant_id: str,
) -> bool:
    """
    若达阈值则拉取 transcript、调用 LLM 生成摘要并 create_summary。

    若已有摘要，将旧摘要并入 prompt 做增量折叠，减少长会话信息丢失。

    Returns:
        是否写入了一条新摘要
    """
    store = memory_manager._store
    msg_count = store.get_message_count(session_id, tenant_id=tenant_id)
    latest = store.get_latest_summary(session_id, tenant_id=tenant_id)

    if not should_run_session_summary(msg_count, latest):
        return False

    rows = store.get_messages(
        session_id,
        limit=SUMMARY_TRANSCRIPT_LIMIT,
        tenant_id=tenant_id,
    )
    transcript = _format_transcript(rows)
    if not transcript.strip():
        return False

    auto = AutoSummary()
    recent_prompt = auto.build_summary_prompt(
        [
            {"role": m.get("role", "user"), "content": m.get("content", "")}
            for m in rows
            if m.get("role") in ("user", "assistant")
        ]
    )

    previous = ""
    if latest:
        previous = (latest.get("summary") or "").strip()[:PREVIOUS_SUMMARY_MAX_CHARS]

    user_blocks: list[str] = []
    if previous:
        user_blocks.append(
            "【上轮以来的会话摘要（请保留仍适用的要点，与新对话合并；勿大段重复）】\n"
            + previous
        )
    user_blocks.append("【近期对话片段】\n" + recent_prompt)
    full_user = "\n\n".join(user_blocks)

    messages = [
        LLMMessage(
            role="system",
            content=(
                "你是对话整理助手。基于【旧摘要（若有）】与【近期对话】输出一份更新后的"
                "精简中文摘要，不要标题套话，400字以内。"
            ),
        ),
        LLMMessage(role="user", content=full_user),
    ]

    try:
        start = time.time()
        registry = get_llm_registry()
        resp = await registry.chat(messages=messages, adapter_name=adapter_name, tools=[])
        latency_ms = int((time.time() - start) * 1000)
        summary_text = (resp.content or "").strip()
        if not summary_text:
            warning("[memory] 会话摘要 LLM 返回为空，跳过写入")
            return False

        record_token_usage(
            agent_id=agent_id,
            provider=resp.provider.value,
            model=resp.model,
            prompt_tokens=resp.prompt_tokens,
            completion_tokens=resp.completion_tokens,
            latency_ms=latency_ms,
        )

        memory_manager.create_summary(
            summary_text,
            session_id=session_id,
            tenant_id=tenant_id,
        )
        info(
            f"[memory] 已写入会话摘要 session={session_id[:16]}… "
            f"messages={msg_count} len={len(summary_text)}"
        )
        return True
    except Exception as e:
        error(f"[memory] 会话摘要失败: {e}")
        return False


async def run_post_turn_memory_maintenance(
    *,
    memory_manager: MemoryManager,
    adapter_name: Optional[str],
    agent_id: str,
    session_id: str,
    tenant_id: str,
    user_id: str,
    load_history: bool,
) -> None:
    """
    在 assistant 回复已写入 SQLite 之后调用（可放后台任务）。
    使用显式 session_id / tenant_id / user_id，避免与共享 MemoryManager 竞态串会话。

    闸门拆分：LLM 会话摘要仅在与私聊拉历史且已配置 adapter 时执行；规则要点抽取与
    MEMORY.md 晋升在群聊（load_history=False）时仍会运行，保证偏好入库。
    """
    if adapter_name and load_history:
        await maybe_refresh_session_summary_with_llm(
            memory_manager=memory_manager,
            adapter_name=adapter_name,
            agent_id=agent_id,
            session_id=session_id,
            tenant_id=tenant_id,
        )

    try:
        msg_count = memory_manager._store.get_message_count(session_id, tenant_id=tenant_id)
        if msg_count > 0 and msg_count % AUTO_EXTRACT_EVERY_N_MESSAGES == 0:
            extract_cap = (
                GROUP_CHAT_EXTRACT_MESSAGES
                if not load_history
                else PRIVATE_CHAT_EXTRACT_MESSAGES
            )
            n = memory_manager.auto_extract_memory_notes(
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                messages_limit=extract_cap,
            )
            if n:
                info(f"[memory] 自动抽取记忆要点 {n} 条 (user={user_id[:12]}…)")
            pn = promote_notes_to_longterm_md(memory_manager, user_id=user_id)
            if pn:
                info(f"[memory] 晋升 {pn} 条记忆要点到 MEMORY.md")
    except Exception as e:
        error(f"[memory] 自动记忆要点抽取 / 晋升失败: {e}")


__all__ = [
    "AUTO_EXTRACT_EVERY_N_MESSAGES",
    "GROUP_CHAT_EXTRACT_MESSAGES",
    "PRIVATE_CHAT_EXTRACT_MESSAGES",
    "MEMORY_MAINT_DEBOUNCE_SEC",
    "PROMOTE_SOFT_MIN_IMPORTANCE",
    "PROMOTE_TO_LONGTERM_MAX_PER_TURN",
    "PROMOTE_TO_LONGTERM_MIN_IMPORTANCE",
    "SUMMARY_FIRST_THRESHOLD",
    "SUMMARY_REPEAT_MIN_NEW",
    "maybe_refresh_session_summary_with_llm",
    "promote_notes_to_longterm_md",
    "run_post_turn_memory_maintenance",
    "should_run_session_summary",
]
