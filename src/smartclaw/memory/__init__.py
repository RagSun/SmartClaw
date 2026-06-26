"""
记忆系统模块

提供 4 层记忆架构：
1. SessionMemory - 会话记忆（当前会话完整历史）
2. DailyMemory - 日常记忆（每日记录总结）
3. LongTermMemory - 长期记忆（重要要点、用户画像）
4. VectorMemory - 向量记忆（语义检索，可选）

Token 预算管理：
- ContextBudget - Token 预算分配

统一接口：
- MemoryManager - 记忆管理器（推荐使用）
- run_post_turn_memory_maintenance - 回合结束后刷新会话摘要 / 记忆要点抽取（由 AgentRunner 调用）
"""

from smartclaw.memory.budget import ContextBudget, TokenBudget, count_tokens
from smartclaw.memory.context_helpers import compact_prefix_from_memory_context
from smartclaw.memory.fts_query import fts5_phrase_query
from smartclaw.memory.daily import DailyMemory
from smartclaw.memory.longterm import LongTermMemory
from smartclaw.memory.manager import MemoryManager
from smartclaw.memory.session import Message, Session, SessionMemory
from smartclaw.memory.session_maintainer import (
    MEMORY_MAINT_DEBOUNCE_SEC,
    promote_notes_to_longterm_md,
    run_post_turn_memory_maintenance,
)

__all__ = [
    # 统一接口
    "MemoryManager",
    "MEMORY_MAINT_DEBOUNCE_SEC",
    "run_post_turn_memory_maintenance",
    "promote_notes_to_longterm_md",
    "compact_prefix_from_memory_context",
    "fts5_phrase_query",
    # 会话记忆
    "SessionMemory",
    "Session",
    "Message",
    # 日常记忆
    "DailyMemory",
    # 长期记忆
    "LongTermMemory",
    # Token 预算
    "ContextBudget",
    "TokenBudget",
    "count_tokens",
]
