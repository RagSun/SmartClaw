"""后台任务查询与管理工具。"""

from __future__ import annotations

from typing import Any

from smartclaw.agent.bg_registry import (
    get_task,
    kill_task,
    list_tasks,
    read_task_log,
    remove_task,
)
from smartclaw.auth.tool_gate import get_tool_security_context


BACKGROUND_TASK_TOOL_DEFINITION = {
    "name": "background_task",
    "description": (
        "管理 execute 自动后台启动的长驻服务。可 list/status/log/kill/clear；"
        "优先用 execute 返回的 [bg] id 查询状态，不要反复 type .log。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "操作: list/status/log/kill/clear",
                "enum": ["list", "status", "log", "kill", "clear"],
            },
            "task_id": {
                "type": "string",
                "description": "后台任务 id，例如 execute 返回的 bg_xxxxxxxxxx",
            },
            "lines": {
                "type": "integer",
                "description": "log 操作返回最近多少行，默认 80，最大 1000",
                "default": 80,
            },
            "include_finished": {
                "type": "boolean",
                "description": "list 时是否包含已完成/失败/终止任务，默认 true",
                "default": True,
            },
        },
        "required": ["action"],
    },
}


def _require_task_id(action: str, task_id: str | None) -> str | None:
    if task_id and task_id.strip():
        return task_id.strip()
    if action in {"status", "log", "kill", "clear"}:
        return None
    return ""


def _task_visible_to_current_context(task: Any) -> bool:
    """Return whether the current tenant/agent may inspect this task."""
    ctx = get_tool_security_context()
    if not ctx:
        return True
    if getattr(task, "tenant_id", "default") != ctx.tenant_id:
        return False
    task_agent = getattr(task, "agent_id", "")
    return not task_agent or task_agent == ctx.agent_id


def background_task_handler(
    action: str,
    task_id: str | None = None,
    lines: int | None = 80,
    include_finished: bool | None = True,
) -> dict[str, Any]:
    action = (action or "").strip().lower()
    if action not in {"list", "status", "log", "kill", "clear"}:
        return {"success": False, "error": f"未知 action: {action}"}

    resolved_id = _require_task_id(action, task_id)
    if resolved_id is None:
        return {"success": False, "error": f"{action} 需要 task_id"}

    if action == "list":
        ctx = get_tool_security_context()
        tasks = list_tasks(
            include_finished=include_finished is not False,
            tenant_id=getattr(ctx, "tenant_id", None) if ctx else None,
            agent_id=getattr(ctx, "agent_id", None) if ctx else None,
        )
        lines_out = []
        for task in tasks:
            url = f" | {task['url']}" if task.get("url") else ""
            pid = f"pid={task['pid']}" if task.get("pid") else "pid=n/a"
            lines_out.append(
                f"{task['id']} {task['status']} {pid} log={task['rel_log']}{url}"
            )
        return {
            "success": True,
            "tasks": tasks,
            "output": "\n".join(lines_out) if lines_out else "No background tasks.",
        }

    if action == "status":
        task = get_task(resolved_id)
        if not task:
            return {"success": False, "error": f"后台任务不存在: {resolved_id}"}
        if not _task_visible_to_current_context(task):
            return {"success": False, "error": f"无权访问后台任务: {resolved_id}"}
        data = task.to_dict()
        return {
            "success": True,
            "task": data,
            "output": (
                f"{data['id']} {data['status']} pid={data.get('pid') or 'n/a'} "
                f"log={data['rel_log']}"
                + (f" {data['url']}" if data.get("url") else "")
            ),
        }

    if action == "log":
        task = get_task(resolved_id)
        if not task:
            return {"success": False, "error": f"后台任务不存在: {resolved_id}"}
        if not _task_visible_to_current_context(task):
            return {"success": False, "error": f"无权访问后台任务: {resolved_id}"}
        return read_task_log(resolved_id, lines=lines or 80)

    if action == "kill":
        task = get_task(resolved_id)
        if not task:
            return {"success": False, "error": f"后台任务不存在: {resolved_id}"}
        if not _task_visible_to_current_context(task):
            return {"success": False, "error": f"无权访问后台任务: {resolved_id}"}
        return kill_task(resolved_id)

    task = get_task(resolved_id)
    if task and not _task_visible_to_current_context(task):
        return {"success": False, "error": f"无权访问后台任务: {resolved_id}"}
    if task and task.status == "running":
        return {
            "success": False,
            "error": f"后台任务仍在运行: {resolved_id}。请先使用 action=kill。",
            "task": task.to_dict(),
        }
    removed = remove_task(resolved_id)
    return {
        "success": removed,
        "message": f"已清理后台任务 {resolved_id}" if removed else "",
        "error": "" if removed else f"后台任务不存在: {resolved_id}",
    }


__all__ = ["BACKGROUND_TASK_TOOL_DEFINITION", "background_task_handler"]
