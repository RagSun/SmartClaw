"""Operator-facing tool catalog and policy inspection tool."""

from __future__ import annotations

from typing import Any

from smartclaw.auth.tool_gate import check_tool_allowed, get_tool_security_context
from smartclaw.config.loader import get_config


def _policy_for_tool(tool_name: str) -> dict[str, Any]:
    """Return the current role-gate decision for a tool and security context."""
    cfg = get_config()
    ctx = get_tool_security_context()
    required_roles = (getattr(cfg.auth, "tool_required_roles_any", None) or {}).get(tool_name, [])
    if not ctx:
        return {
            "has_context": False,
            "required_roles_any": required_roles,
            "allowed": True,
            "reason": "no tool security context; registry execution may run outside channel request",
        }
    ok, reason = check_tool_allowed(tool_name, ctx, cfg)
    return {
        "has_context": True,
        "tenant_id": ctx.tenant_id,
        "agent_id": ctx.agent_id,
        "session_id": ctx.session_id,
        "roles": list(ctx.roles),
        "required_roles_any": required_roles,
        "allowed": ok,
        "reason": reason,
    }


def tool_audit_handler(
    action: str = "list",
    tool_name: str | None = None,
    include_schema: bool = False,
) -> dict[str, Any]:
    """Inspect registered tools, metadata and current policy decisions.

    Actions:
        list: Return compact rows for every registered tool.
        describe: Return full details for one tool.
        policy: Return current tenant/user role decision for one or all tools.
    """
    from smartclaw.agent.tools.registry import get_tool_registry

    registry = get_tool_registry()
    act = (action or "list").strip().lower()

    if act in {"list", "catalog"}:
        rows = registry.list_catalog()
        if not include_schema:
            for row in rows:
                row.pop("parameters", None)
        return {"success": True, "tools": rows, "total": len(rows)}

    if act == "describe":
        if not tool_name:
            return {"success": False, "error": "describe 需要 tool_name"}
        row = registry.describe(tool_name)
        if not row:
            return {"success": False, "error": f"工具不存在: {tool_name}"}
        if not include_schema:
            row.pop("parameters", None)
        row["policy"] = _policy_for_tool(tool_name)
        return {"success": True, "tool": row}

    if act == "policy":
        if tool_name:
            if not registry.get(tool_name):
                return {"success": False, "error": f"工具不存在: {tool_name}"}
            return {"success": True, "policy": {tool_name: _policy_for_tool(tool_name)}}
        return {
            "success": True,
            "policy": {row["name"]: _policy_for_tool(row["name"]) for row in registry.list_catalog()},
        }

    return {"success": False, "error": f"未知 action: {action}"}


TOOL_AUDIT_TOOL_DEFINITION = {
    "name": "tool_audit",
    "description": "列出/描述内置工具的原子 metadata、schema 和当前租户角色策略。",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "list/catalog/describe/policy，默认 list"},
            "tool_name": {"type": "string", "description": "describe/policy 的工具名"},
            "include_schema": {"type": "boolean", "description": "是否返回 JSON Schema，默认 false"},
        },
        "required": [],
    },
}


__all__ = ["TOOL_AUDIT_TOOL_DEFINITION", "tool_audit_handler"]
