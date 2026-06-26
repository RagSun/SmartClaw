"""MCP 远端工具 → 本进程 ToolRegistry 的桥接（与扩展包管理、宿主命令策略区分开）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from smartclaw.agent.tools.registry import ToolRegistry, get_tool_registry
from smartclaw.auth.tool_gate import get_tool_security_context
from smartclaw.config.loader import McpServerConfig, get_config
from smartclaw.console import info, warning


@dataclass(frozen=True)
class McpToolSpec:
    server_key: str
    server_name: str
    raw_name: str
    registry_name: str
    description: str
    parameters: dict[str, Any]


def _safe_tool_part(value: str) -> str:
    out = "".join(c if c.isalnum() or c == "_" else "_" for c in (value or ""))
    out = out.strip("_")
    return out or "tool"


def _registry_tool_name(server_name: str, raw_name: str) -> str:
    return f"{_safe_tool_part(server_name)}__{_safe_tool_part(raw_name)}"


def _agent_mcp_servers(agent_cfg: dict[str, Any]) -> set[str]:
    mcp_cfg = agent_cfg.get("mcp") if isinstance(agent_cfg.get("mcp"), dict) else {}
    value = mcp_cfg.get("servers") or agent_cfg.get("mcp_servers") or []
    if isinstance(value, str):
        return {item.strip() for item in value.split(",") if item.strip()}
    if isinstance(value, (list, tuple, set)):
        return {str(item).strip() for item in value if str(item).strip()}
    return set()


def _tool_attr(tool: Any, *names: str, default: Any = None) -> Any:
    if isinstance(tool, dict):
        for name in names:
            if name in tool:
                return tool[name]
        return default
    for name in names:
        if hasattr(tool, name):
            return getattr(tool, name)
    return default


def _schema_from_tool(tool: Any) -> dict[str, Any]:
    schema = _tool_attr(
        tool,
        "inputSchema",
        "input_schema",
        "parameters",
        "schema",
        default=None,
    )
    if hasattr(schema, "model_dump"):
        schema = schema.model_dump()
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    if schema.get("type") == "object":
        return schema
    return {"type": "object", "properties": schema.get("properties") or {}}


def _normalize_mcp_result(result: Any) -> Any:
    if hasattr(result, "model_dump"):
        result = result.model_dump()
    if isinstance(result, list):
        normalized: list[Any] = []
        for item in result:
            if hasattr(item, "model_dump"):
                item = item.model_dump()
            normalized.append(item)
        return normalized
    return result


async def _list_server_tools(
    server_key: str,
    server: McpServerConfig,
) -> list[McpToolSpec]:
    try:
        from fastmcp import Client
    except ImportError as exc:
        raise RuntimeError("缺少 fastmcp 依赖，请安装 smartclaw[mcp] 或 uv pip install fastmcp") from exc

    if server.transport not in {"sse", "http"}:
        raise RuntimeError(f"暂不支持 MCP transport={server.transport!r}（当前教学闭环使用 sse/http）")
    if not server.url:
        raise RuntimeError(f"MCP server {server_key} 缺少 url")

    server_name = server.name or server_key
    async with Client(server.url) as client:
        tools = await client.list_tools()
    specs: list[McpToolSpec] = []
    for tool in tools or []:
        raw_name = str(_tool_attr(tool, "name", default="")).strip()
        if not raw_name:
            continue
        specs.append(
            McpToolSpec(
                server_key=server_key,
                server_name=server_name,
                raw_name=raw_name,
                registry_name=_registry_tool_name(server_name, raw_name),
                description=str(_tool_attr(tool, "description", default="") or raw_name),
                parameters=_schema_from_tool(tool),
            )
        )
    return specs


def _context_payload() -> dict[str, Any]:
    ctx = get_tool_security_context()
    if not ctx:
        return {}
    return {
        "tenant_id": ctx.tenant_id,
        "agent_id": ctx.agent_id,
        "user_open_id": ctx.feishu_open_id,
        "roles": list(ctx.roles or ()),
        "session_id": ctx.session_id,
    }


def _make_mcp_handler(server: McpServerConfig, raw_tool_name: str) -> Any:
    async def handler(**kwargs: Any) -> Any:
        try:
            from fastmcp import Client
        except ImportError as exc:
            raise RuntimeError("缺少 fastmcp 依赖，请安装 smartclaw[mcp] 或 uv pip install fastmcp") from exc

        arguments = {k: v for k, v in kwargs.items() if v is not None}
        if server.context_argument:
            arguments[server.context_argument] = _context_payload()
        async with Client(server.url) as client:
            result = await client.call_tool(raw_tool_name, arguments)
        return _normalize_mcp_result(result)

    return handler


async def register_mcp_tools_for_agent(
    agent_cfg: dict[str, Any],
    *,
    tenant_id: str,
    registry: ToolRegistry | None = None,
) -> dict[str, Any]:
    """Register MCP tools enabled by the current Agent config.

    Agent config controls which MCP servers are visible. Tool-level permission is
    still enforced later by ToolRegistry allowed_tools / role / confirmation gates.
    """
    cfg = get_config()
    mcp_cfg = getattr(cfg, "mcp", None)
    requested = _agent_mcp_servers(agent_cfg)
    loaded: list[str] = []
    skipped: list[dict[str, str]] = []
    if not mcp_cfg or not getattr(mcp_cfg, "enabled", False):
        return {"success": True, "loaded": loaded, "skipped": skipped}
    if not requested:
        return {"success": True, "loaded": loaded, "skipped": skipped}

    reg = registry or get_tool_registry()
    for server_key in sorted(requested):
        server = (mcp_cfg.servers or {}).get(server_key)
        if not server:
            skipped.append({"server": server_key, "reason": "not_configured"})
            continue
        if not server.enabled:
            skipped.append({"server": server_key, "reason": "disabled"})
            continue
        try:
            specs = await _list_server_tools(server_key, server)
            for spec in specs:
                reg.register(
                    name=spec.registry_name,
                    description=(
                        f"[MCP:{spec.server_name}] {spec.description} "
                        f"(raw tool: {spec.raw_name})"
                    ),
                    handler=_make_mcp_handler(server, spec.raw_name),
                    parameters=spec.parameters,
                    timeout_ms=server.timeout_ms,
                    metadata={
                        "owner": "mcp",
                        "version": "0.1.0",
                        "risk_level": server.risk_level,
                        "tenant_scope": server.tenant_scope,
                        "requires_confirmation": server.requires_confirmation,
                        "mcp_server": spec.server_key,
                        "mcp_server_name": spec.server_name,
                        "mcp_raw_tool": spec.raw_name,
                        "tenant_id": tenant_id,
                    },
                )
                loaded.append(spec.registry_name)
        except Exception as exc:
            warning(f"[MCP] 注册 server={server_key} 工具失败: {exc}")
            skipped.append({"server": server_key, "reason": str(exc)})
    if loaded or skipped:
        info(
            "[MCP] tools registered | "
            f"tenant={tenant_id} loaded={len(loaded)} skipped={len(skipped)}"
        )
    return {"success": True, "loaded": loaded, "skipped": skipped}
