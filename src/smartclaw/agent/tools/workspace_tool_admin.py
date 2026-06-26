"""Tools for reloading workspace-defined tools."""

from __future__ import annotations

from typing import Any

from smartclaw.agent.tools.registry import get_tool_registry
from smartclaw.agent.tools.workspace_tool_loader import register_workspace_tools
from smartclaw.agent.workspace import resolve_agent_workspace_dir
from smartclaw.auth.tool_gate import get_tool_security_context
from smartclaw.config.loader import get_config
from smartclaw.tenant import DEFAULT_TENANT_ID, normalize_tenant_id


async def reload_workspace_tools_handler(agent_id: str = "", tenant_id: str = "") -> dict[str, Any]:
    """Reload ``workspace/tools/*/tool.json`` into ToolRegistry."""
    ctx = get_tool_security_context()
    tenant = normalize_tenant_id(tenant_id or (ctx.tenant_id if ctx else DEFAULT_TENANT_ID))
    agent = (agent_id or (ctx.agent_id if ctx else "") or "default").strip()
    ws = resolve_agent_workspace_dir(agent, {"tenant_id": tenant}, get_config(), tenant_id=tenant)
    result = register_workspace_tools(ws, registry=get_tool_registry())
    result["agent_id"] = agent
    result["tenant_id"] = tenant
    result["output"] = (
        f"已加载 {len(result.get('loaded', []))} 个 workspace tools；"
        f"跳过 {len(result.get('skipped', []))} 个。"
    )
    return result


async def workspace_tool_status_handler(agent_id: str = "", tenant_id: str = "") -> dict[str, Any]:
    """List workspace tool manifests and whether they are registered."""
    ctx = get_tool_security_context()
    tenant = normalize_tenant_id(tenant_id or (ctx.tenant_id if ctx else DEFAULT_TENANT_ID))
    agent = (agent_id or (ctx.agent_id if ctx else "") or "default").strip()
    ws = resolve_agent_workspace_dir(agent, {"tenant_id": tenant}, get_config(), tenant_id=tenant)
    tools_root = ws / "tools"
    reg = get_tool_registry()
    rows: list[dict[str, Any]] = []
    if tools_root.is_dir():
        for manifest in sorted(tools_root.glob("*/tool.json")):
            try:
                import json

                data = json.loads(manifest.read_text(encoding="utf-8"))
                name = str(data.get("name") or manifest.parent.name)
                rows.append(
                    {
                        "name": name,
                        "path": str(manifest),
                        "enabled": data.get("enabled", True),
                        "registered": bool(reg.get(name)),
                    }
                )
            except Exception as exc:
                rows.append({"name": manifest.parent.name, "path": str(manifest), "error": str(exc)})
    return {
        "success": True,
        "agent_id": agent,
        "tenant_id": tenant,
        "workspace": str(ws),
        "tools": rows,
        "count": len(rows),
    }


RELOAD_WORKSPACE_TOOLS_DEFINITION = {
    "name": "reload_workspace_tools",
    "description": "把当前 Agent 工作区 tools/<name>/tool.json + handler.py 注册为正式 ToolRegistry 工具。",
    "parameters": {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "可选 Agent 名，默认当前 Agent"},
            "tenant_id": {"type": "string", "description": "可选租户 ID，默认当前 tenant"},
        },
    },
}

WORKSPACE_TOOL_STATUS_DEFINITION = {
    "name": "workspace_tool_status",
    "description": "查看当前 Agent workspace/tools 下的工具 manifest 及注册状态。",
    "parameters": {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "可选 Agent 名，默认当前 Agent"},
            "tenant_id": {"type": "string", "description": "可选租户 ID，默认当前 tenant"},
        },
    },
}


__all__ = [
    "RELOAD_WORKSPACE_TOOLS_DEFINITION",
    "WORKSPACE_TOOL_STATUS_DEFINITION",
    "reload_workspace_tools_handler",
    "workspace_tool_status_handler",
]
