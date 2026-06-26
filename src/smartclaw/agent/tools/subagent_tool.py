"""Formal tools for spawning and tracking sub-agent jobs."""

from __future__ import annotations

from typing import Any

from smartclaw.auth.tool_gate import get_tool_security_context
from smartclaw.core.subagent_registry import SubagentRegistry, SubagentStatus
from smartclaw.core.subagent_spawn import SpawnConfig, SubagentSpawner


_SPAWNERS: dict[str, SubagentSpawner] = {}


def set_subagent_spawner(agent_key: str, spawner: SubagentSpawner) -> None:
    key = (agent_key or "default").strip() or "default"
    _SPAWNERS[key] = spawner


def _registry() -> SubagentRegistry:
    return SubagentRegistry()


def _current_spawner() -> SubagentSpawner | None:
    ctx = get_tool_security_context()
    keys = []
    if ctx:
        if ctx.tenant_id and ctx.agent_id and ctx.tenant_id != "default":
            keys.append(f"{ctx.tenant_id}/{ctx.agent_id}")
        if ctx.agent_id:
            keys.append(ctx.agent_id)
    keys.append("default")
    for key in keys:
        if key in _SPAWNERS:
            return _SPAWNERS[key]
    return None


def _run_to_dict(run: Any) -> dict[str, Any]:
    if hasattr(run, "to_dict"):
        return run.to_dict()
    return {
        "run_id": getattr(run, "job_id", ""),
        "task": getattr(run, "task", ""),
        "status": getattr(getattr(run, "status", ""), "value", getattr(run, "status", "")),
        "result_text": getattr(run, "result", None),
        "error": getattr(run, "error", None),
    }


async def spawn_subagent_handler(
    task: str,
    timeout_seconds: int | None = 300,
    agent_id: str | None = None,
    mode: str = "run",
) -> dict[str, Any]:
    """Spawn a background sub-agent job."""
    text = (task or "").strip()
    if not text:
        return {"success": False, "error": "task 不能为空"}
    spawner = _current_spawner()
    if not spawner:
        return {"success": False, "error": "SubagentSpawner 未初始化"}

    ctx = get_tool_security_context()
    resolved_agent = (agent_id or (ctx.agent_id if ctx else "") or "default").strip()

    from smartclaw.agent.manager import AgentManager
    from smartclaw.tenant import normalize_tenant_id

    tid = normalize_tenant_id(ctx.tenant_id if ctx else "default")
    try:
        target_cfg = AgentManager()._read_config(resolved_agent, tenant_id=tid)
    except Exception:
        target_cfg = None
    if not target_cfg:
        return {
            "success": False,
            "error": (
                f"agent_id={resolved_agent!r} 在租户 {tid!r} 下不存在或不可读。"
                " 请在同租户内使用已配置的 Agent，或省略 agent_id 以使用当前 Agent。"
            ),
        }

    cfg = SpawnConfig(
        task=text,
        agent_id=resolved_agent,
        mode=(mode or "run").strip() or "run",
        timeout_seconds=max(1, min(int(timeout_seconds or 300), 3600)),
        tenant_id=(ctx.tenant_id if ctx else "default"),
        user_id=(ctx.feishu_open_id if ctx else ""),
        roles=tuple(ctx.roles if ctx else ("default",)),
        integration_env=tuple(ctx.integration_env if ctx else ()),
    )
    result = await spawner.spawn(
        cfg,
        requester_session_key=(ctx.session_id if ctx else ""),
        requester_agent_id=(ctx.agent_id if ctx else resolved_agent),
    )
    return {
        "success": result.status == "accepted",
        "status": result.status,
        "job_id": result.job_id,
        "note": result.note,
        "error": result.error,
        "output": result.note or result.error or "",
    }


async def subagent_status_handler(
    job_id: str | None = None,
    session_id: str | None = None,
    all: bool | None = False,
) -> dict[str, Any]:
    """Return one sub-agent job, jobs for a session, or active jobs."""
    reg = _registry()
    jid = (job_id or "").strip()
    if jid:
        run = reg.get(jid)
        if not run:
            spawner = _current_spawner()
            run = await spawner.check(jid) if spawner else None
        return {
            "success": bool(run),
            "job": _run_to_dict(run) if run else None,
            "error": "" if run else f"未找到子 Agent 任务: {jid}",
        }

    sid = (session_id or "").strip()
    if not sid:
        ctx = get_tool_security_context()
        sid = ctx.session_id if ctx else ""
    if sid:
        runs = reg.list_for_requester(sid)
    elif all:
        runs = list(reg._runs.values())
    else:
        runs = reg.list_active()

    return {
        "success": True,
        "jobs": [_run_to_dict(r) for r in runs],
        "count": len(runs),
    }


async def subagent_cancel_handler(job_id: str) -> dict[str, Any]:
    """Cancel/mark killed a sub-agent job."""
    jid = (job_id or "").strip()
    if not jid:
        return {"success": False, "error": "job_id 不能为空"}
    spawner = _current_spawner()
    ok = await spawner.cancel(jid) if spawner else False
    if not ok:
        run = _registry().get(jid)
        if run and run.status in {SubagentStatus.PENDING, SubagentStatus.RUNNING}:
            _registry().mark_killed(jid)
            ok = True
    return {
        "success": ok,
        "job_id": jid,
        "output": "已取消子 Agent 任务" if ok else "任务不存在或已结束",
    }


SPAWN_SUBAGENT_TOOL_DEFINITION = {
    "name": "spawn_subagent",
    "description": (
        "派生后台子 Agent（多任务并行模型的标准出口），适合长耗时 shell/安装/运行/探索；返回 job_id。"
        " 继承当前请求的 tenant、飞书身份、角色门禁与集成环境；DeepAgents 内置 execute 与 Registry exec 走同一套宿主白名单与 Agent 工具策略。"
        " agent_id 仅允许传本租户已存在且可读的 Agent；省略则等同于当前 Agent。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "任务说明；执行命令建议写为：执行命令: <shell>"},
            "timeout_seconds": {"type": "integer", "description": "超时秒数，默认 300，最大 3600"},
            "agent_id": {
                "type": "string",
                "description": "可选；须为当前租户下已存在的 Agent 名，省略则用当前 Agent（配置/工作区随之切换）",
            },
            "mode": {"type": "string", "description": "run/session，当前默认 run"},
        },
        "required": ["task"],
    },
}

SUBAGENT_STATUS_TOOL_DEFINITION = {
    "name": "subagent_status",
    "description": "查询子 Agent 任务状态；可按 job_id、当前 session 或 active/all 查询。",
    "parameters": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "可选，指定任务 ID"},
            "session_id": {"type": "string", "description": "可选，父会话 ID；默认当前会话"},
            "all": {"type": "boolean", "description": "是否列出所有记录，默认 false"},
        },
    },
}

SUBAGENT_CANCEL_TOOL_DEFINITION = {
    "name": "subagent_cancel",
    "description": "取消或标记终止一个子 Agent 任务。",
    "parameters": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "任务 ID"},
        },
        "required": ["job_id"],
    },
}


__all__ = [
    "SPAWN_SUBAGENT_TOOL_DEFINITION",
    "SUBAGENT_CANCEL_TOOL_DEFINITION",
    "SUBAGENT_STATUS_TOOL_DEFINITION",
    "set_subagent_spawner",
    "spawn_subagent_handler",
    "subagent_cancel_handler",
    "subagent_status_handler",
]
