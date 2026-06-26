"""飞书用户 → 角色解析、工具级门禁、租户集成环境（ContextVar）。"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from smartclaw.config.loader import Config


@dataclass(frozen=True)
class ToolSecurityContext:
    tenant_id: str
    feishu_open_id: str
    roles: tuple[str, ...]
    agent_id: str = ""
    session_id: str = ""
    integration_env: tuple[tuple[str, str], ...] = ()


_tool_ctx: ContextVar[Optional[ToolSecurityContext]] = ContextVar("smartclaw_tool_security", default=None)


def get_tool_security_context() -> Optional[ToolSecurityContext]:
    return _tool_ctx.get()


def set_tool_security_context(ctx: ToolSecurityContext) -> Any:
    return _tool_ctx.set(ctx)


def reset_tool_security_context(token: Any) -> None:
    _tool_ctx.reset(token)


def resolve_feishu_roles(tenant_id: str, open_id: str, cfg: "Config") -> list[str]:
    m = getattr(cfg.auth, "feishu_open_id_roles_by_tenant", None) or {}
    inner = m.get(tenant_id) or m.get("*") or {}
    if open_id in inner:
        return list(inner[open_id])
    if "*" in inner:
        return list(inner["*"])
    return ["default"]


def check_tool_allowed(tool_name: str, ctx: ToolSecurityContext, cfg: "Config") -> tuple[bool, str]:
    req = dict(DEFAULT_HIGH_RISK_TOOL_ROLES)
    req.update(getattr(cfg.auth, "tool_required_roles_any", None) or {})
    if tool_name not in req:
        return True, ""
    needed = set(req[tool_name])
    user_roles = set(ctx.roles)
    if needed & user_roles:
        return True, ""
    return False, f"工具 {tool_name} 需要以下角色之一: {sorted(needed)}"


def check_shell_capability_allowed(ctx: ToolSecurityContext, cfg: "Config") -> tuple[bool, str]:
    """
    DeepAgents ``execute`` 与 Registry ``exec`` 共享 Shell 能力。
    ``tool_required_roles_any``（及默认表）中为 ``exec`` / ``execute`` 分别配置的
    角色要求各自须满足至少其一（单独某键为空列表时表示该键不要求角色）。
    """
    req = dict(DEFAULT_HIGH_RISK_TOOL_ROLES)
    req.update(getattr(cfg.auth, "tool_required_roles_any", None) or {})
    user_roles = set(ctx.roles)
    for key in ("exec", "execute"):
        if key not in req:
            continue
        needed = set(req[key])
        if not needed:
            continue
        if not (needed & user_roles):
            return False, (
                f"Shell 能力（DeepAgents execute / Registry exec）关联工具 {key!r}："
                f"需要以下角色之一 {sorted(needed)}"
            )
    return True, ""


DEFAULT_HIGH_RISK_TOOL_ROLES: dict[str, list[str]] = {
    "agent_create": ["tenant_admin", "platform_admin"],
    "agent_update_feishu": ["tenant_admin", "platform_admin"],
    "reload_workspace_tools": ["tenant_admin", "platform_admin"],
    "exec": ["tenant_admin", "platform_admin", "developer"],
    "execute": ["tenant_admin", "platform_admin", "developer"],
    "integration_http_request": ["tenant_admin", "platform_admin", "developer"],
    "write_file": ["tenant_admin", "platform_admin", "developer"],
    "edit_file": ["tenant_admin", "platform_admin", "developer"],
    "background_task": ["tenant_admin", "platform_admin", "developer"],
    "spawn_subagent": ["tenant_admin", "platform_admin", "developer"],
}


def default_required_roles_for_tool(tool_name: str) -> list[str]:
    """Return built-in default roles for high-risk tools."""
    return list(DEFAULT_HIGH_RISK_TOOL_ROLES.get(tool_name, []))


def get_tenant_integration_env() -> dict[str, str]:
    """
    当前异步上下文下的租户集成环境变量（来自配置，只读副本）。
    工具内可 merge 到自己的 HTTP client headers / base_url。
    """
    c = get_tool_security_context()
    if not c or not c.integration_env:
        return {}
    return dict(c.integration_env)
