"""Operator tools for creating and inspecting SmartClaw agents."""

from __future__ import annotations

from typing import Any

from smartclaw.agent.manager import AgentManager, CreateAgentRequest, UpdateAgentRequest
from smartclaw.agent.workspace import resolve_agent_workspace_dir
from smartclaw.auth.tool_gate import get_tool_security_context
from smartclaw.config.loader import get_config
from smartclaw.logging_utils import safe_preview
from smartclaw.tenant import DEFAULT_TENANT_ID, normalize_tenant_id, tenant_agent_key


def _ctx_tenant() -> str:
    ctx = get_tool_security_context()
    return normalize_tenant_id(ctx.tenant_id if ctx else DEFAULT_TENANT_ID)


def _agent_info_row(agent: Any) -> dict[str, Any]:
    return {
        "name": agent.name,
        "tenant_id": agent.tenant_id,
        "qualified_name": agent.qualified_name or tenant_agent_key(agent.name, agent.tenant_id),
        "display_name": agent.display_name,
        "description": agent.description,
        "enabled": agent.enabled,
        "channel": agent.channel,
        "app_id_preview": safe_preview(agent.app_id, 20),
        "llm_provider": agent.llm_provider,
        "llm_model": agent.llm_model,
        "sandbox_enabled": agent.sandbox_enabled,
        "sandbox_type": agent.sandbox_type,
        "config_path": agent.config_path,
    }


async def agent_create_handler(
    name: str,
    app_id: str,
    app_secret: str,
    display_name: str = "",
    description: str = "",
    tenant_id: str = "",
    llm_provider: str = "",
    llm_model: str = "glm-5",
    llm_api_key: str = "",
    sandbox_enabled: bool | None = True,
    workspace: str = "",
) -> dict[str, Any]:
    """Create a tenant-scoped agent and scaffold its workspace."""
    tenant = normalize_tenant_id(tenant_id or _ctx_tenant())
    display = (display_name or name).strip()
    manager = AgentManager()
    req = CreateAgentRequest(
        name=name.strip(),
        tenant_id=tenant,
        display_name=display,
        description=description.strip(),
        app_id=app_id.strip(),
        app_secret=app_secret.strip(),
        llm_provider=(llm_provider or "").strip(),
        llm_model=(llm_model or "glm-5").strip(),
        llm_api_key=(llm_api_key or "").strip(),
        sandbox_enabled=bool(sandbox_enabled),
        workspace=(workspace or "").strip(),
    )
    ok, msg, info = manager.create_agent(req)
    payload: dict[str, Any] = {
        "success": ok,
        "message": msg,
        "output": msg,
        "requires_restart": True,
        "restart_note": "新 Feishu App/Agent 创建后，当前 WS/HTTP 服务通常需要重启或重新加载 Agent 才能接收该 App 事件。",
    }
    if info:
        payload["agent"] = _agent_info_row(info)
        cfg = manager._read_config(info.name, tenant_id=info.tenant_id) or {}
        payload["workspace_dir"] = str(
            resolve_agent_workspace_dir(info.name, cfg, get_config(), tenant_id=info.tenant_id)
        )
    return payload


async def agent_update_feishu_handler(
    name: str,
    app_id: str = "",
    app_secret: str = "",
    tenant_id: str = "",
) -> dict[str, Any]:
    """Update Feishu credentials for an existing agent."""
    tenant = normalize_tenant_id(tenant_id or _ctx_tenant())
    req = UpdateAgentRequest(
        tenant_id=tenant,
        app_id=(app_id or None),
        app_secret=(app_secret or None),
    )
    ok, msg = AgentManager().update_agent(name.strip(), req)
    return {
        "success": ok,
        "message": msg,
        "output": msg,
        "requires_restart": True,
        "restart_note": "更新 Feishu 凭证后，运行中的 Feishu worker 通常需要重启或重新加载。",
    }


async def agent_status_handler(name: str = "", tenant_id: str = "") -> dict[str, Any]:
    """List agents or show one agent without exposing secrets."""
    manager = AgentManager()
    tenant = normalize_tenant_id(tenant_id or _ctx_tenant())
    raw = (name or "").strip()
    if raw:
        ref = raw if "/" in raw or tenant == DEFAULT_TENANT_ID else f"{tenant}/{raw}"
        agent = manager.get_agent(ref)
        return {
            "success": bool(agent),
            "agent": _agent_info_row(agent) if agent else None,
            "error": "" if agent else f"Agent 不存在: {ref}",
        }
    rows = [
        _agent_info_row(agent)
        for agent in manager.list_agents()
        if not tenant_id or agent.tenant_id == tenant
    ]
    return {"success": True, "agents": rows, "count": len(rows)}


AGENT_CREATE_TOOL_DEFINITION = {
    "name": "agent_create",
    "description": "创建一个租户隔离的 SmartClaw Agent，并加密保存 Feishu app_secret/LLM api_key。",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Agent 名称，字母/数字/下划线，2-32 位"},
            "app_id": {"type": "string", "description": "飞书 App ID，形如 cli_xxx"},
            "app_secret": {"type": "string", "description": "飞书 App Secret；会加密落盘，禁止回显"},
            "display_name": {"type": "string", "description": "显示名，默认同 name"},
            "description": {"type": "string", "description": "Agent 描述"},
            "tenant_id": {"type": "string", "description": "租户 ID，默认当前会话 tenant"},
            "llm_provider": {
                "type": "string",
                "description": "可选，与 agent set-llm --provider 一致；留空则按 llm_model 自动选网关（如 qwen-plus→百炼）",
            },
            "llm_model": {"type": "string", "description": "LLM 模型名，默认 glm-5"},
            "llm_api_key": {"type": "string", "description": "可选 LLM API Key；会加密落盘"},
            "sandbox_enabled": {"type": "boolean", "description": "是否启用沙箱，默认 true"},
            "workspace": {"type": "string", "description": "可选工作区路径；留空使用租户默认布局"},
        },
        "required": ["name", "app_id", "app_secret"],
    },
}

AGENT_UPDATE_FEISHU_TOOL_DEFINITION = {
    "name": "agent_update_feishu",
    "description": "更新已有 Agent 的 Feishu app_id/app_secret，敏感信息加密保存且不回显。",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Agent 名称，可用 tenant/agent"},
            "app_id": {"type": "string", "description": "可选，新飞书 App ID"},
            "app_secret": {"type": "string", "description": "可选，新飞书 App Secret"},
            "tenant_id": {"type": "string", "description": "可选租户 ID"},
        },
        "required": ["name"],
    },
}

AGENT_STATUS_TOOL_DEFINITION = {
    "name": "agent_status",
    "description": "查看当前租户或指定 Agent 的配置状态；不会返回 app_secret/API key。",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "可选 Agent 名称，可用 tenant/agent"},
            "tenant_id": {"type": "string", "description": "可选租户 ID"},
        },
    },
}


__all__ = [
    "AGENT_CREATE_TOOL_DEFINITION",
    "AGENT_STATUS_TOOL_DEFINITION",
    "AGENT_UPDATE_FEISHU_TOOL_DEFINITION",
    "agent_create_handler",
    "agent_status_handler",
    "agent_update_feishu_handler",
]
