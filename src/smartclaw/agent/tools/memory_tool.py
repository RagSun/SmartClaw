"""Formal memory tools: search/get/write."""

from __future__ import annotations

from typing import Any

from smartclaw.auth.tool_gate import get_tool_security_context
from smartclaw.memory.manager import MemoryManager
from smartclaw.paths import default_memory_data_dir


def _manager(session_id: str | None = None) -> MemoryManager:
    ctx = get_tool_security_context()
    agent_id = (ctx.agent_id if ctx else "") or "default"
    user_id = (ctx.feishu_open_id if ctx else "") or ""
    tenant_id = (ctx.tenant_id if ctx else "") or "default"
    raw_sid = (session_id or "").strip()
    if raw_sid.lower() in {"", "none", "null", "current"}:
        raw_sid = ""
    sid = (raw_sid or (ctx.session_id if ctx else "") or "").strip()
    mm = MemoryManager(
        agent_id=agent_id,
        session_id=sid,
        channel="tool",
        user_id=user_id,
        data_dir=default_memory_data_dir(agent_id, tenant_id),
    )
    mm.tenant_id = tenant_id
    return mm


def memory_search_handler(
    query: str,
    session_id: str | None = None,
    limit: int | None = 5,
) -> dict[str, Any]:
    """Search memory with FTS + optional embedding hybrid."""
    mm = _manager(session_id)
    if not mm.session_id:
        return {
            "success": False,
            "error": "memory_search 需要当前会话 session_id，或显式传入 session_id",
        }
    results = mm.search_memory_hybrid(query, limit=max(1, min(int(limit or 5), 20)))
    return {
        "success": True,
        "query": query,
        "session_id": mm.session_id,
        "results": results,
        "output": "\n".join(
            f"- {r.get('citation')}: {(r.get('body') or '').strip()[:240]}"
            for r in results
        ) or "No memory hits.",
    }


def _parse_citation(citation: str) -> tuple[str, str] | None:
    raw = (citation or "").strip()
    if "#" not in raw:
        return None
    kind, sid = raw.split("#", 1)
    kind = kind.strip()
    sid = sid.strip()
    if kind not in {"message", "summary", "note"} or not sid:
        return None
    return kind, sid


def memory_get_handler(citation: str, session_id: str | None = None) -> dict[str, Any]:
    """Read a specific memory record by citation, e.g. message#12."""
    parsed = _parse_citation(citation)
    if not parsed:
        return {"success": False, "error": "citation 格式应为 message#id / summary#id / note#id"}
    mm = _manager(session_id)
    rec = mm._store.get_memory_record(
        source_kind=parsed[0],
        source_id=parsed[1],
        tenant_id=mm.tenant_id,
        user_id=mm.user_id or "",
        agent_id=mm.agent_id,
    )
    if not rec:
        return {"success": False, "error": f"未找到记忆: {citation}"}
    return {"success": True, "record": rec, "output": rec.get("body") or ""}


def memory_write_handler(
    content: str,
    kind: str = "note",
    importance: int | None = 7,
    note_kind: str | None = "manual",
    key: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Write structured memory as note/profile/summary."""
    mm = _manager(session_id)
    k = (kind or "note").strip().lower()
    text = (content or "").strip()
    if not text:
        return {"success": False, "error": "content 不能为空"}

    if k == "profile":
        profile_key = (key or note_kind or "note").strip()
        mm.update_user_profile(profile_key, text, confidence=max(1, min(int(importance or 7), 10)))
        return {
            "success": True,
            "kind": "profile",
            "key": profile_key,
            "output": f"已写入用户画像: {profile_key}",
        }
    if k == "summary":
        if not mm.session_id:
            return {"success": False, "error": "summary 写入需要 session_id"}
        mm.create_summary(text, session_id=mm.session_id, tenant_id=mm.tenant_id)
        return {"success": True, "kind": "summary", "output": "已写入会话摘要"}

    row_id = mm._store.add_memory_note(
        note_kind=(note_kind or "manual"),
        content=text,
        importance=max(1, min(int(importance or 7), 10)),
        user_id=mm.user_id,
        agent_id=mm.agent_id,
        dedupe=True,
        tenant_id=mm.tenant_id,
    )
    return {
        "success": True,
        "kind": "note",
        "id": row_id,
        "citation": f"note#{row_id}" if row_id else "",
        "output": "已写入记忆要点" if row_id else "记忆已存在，跳过去重写入",
    }


MEMORY_SEARCH_TOOL_DEFINITION = {
    "name": "memory_search",
    "description": "语义/关键词混合检索记忆；涉及历史、偏好、决策、待办、上下文时优先调用。",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索问题或关键词"},
            "session_id": {"type": "string", "description": "可选会话 ID，默认当前会话"},
            "limit": {"type": "integer", "description": "返回条数，默认 5"},
        },
        "required": ["query"],
    },
}

MEMORY_GET_TOOL_DEFINITION = {
    "name": "memory_get",
    "description": "按 citation 读取记忆原文，例如 message#12、summary#3、note#9。",
    "parameters": {
        "type": "object",
        "properties": {
            "citation": {"type": "string", "description": "memory_search 返回的 citation"},
            "session_id": {"type": "string", "description": "可选会话 ID"},
        },
        "required": ["citation"],
    },
}

MEMORY_WRITE_TOOL_DEFINITION = {
    "name": "memory_write",
    "description": "写入结构化记忆：note/profile/summary。只保存稳定偏好、事实、决策或重要结论。",
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "要保存的记忆内容"},
            "kind": {"type": "string", "description": "note/profile/summary，默认 note"},
            "importance": {"type": "integer", "description": "重要性 1-10，默认 7"},
            "note_kind": {"type": "string", "description": "记忆要点类别或 profile 默认 key"},
            "key": {"type": "string", "description": "profile 字段名"},
            "session_id": {"type": "string", "description": "可选会话 ID"},
        },
        "required": ["content"],
    },
}


__all__ = [
    "MEMORY_GET_TOOL_DEFINITION",
    "MEMORY_SEARCH_TOOL_DEFINITION",
    "MEMORY_WRITE_TOOL_DEFINITION",
    "memory_get_handler",
    "memory_search_handler",
    "memory_write_handler",
]
