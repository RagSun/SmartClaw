"""Helpers for business tools to use the current tenant-scoped context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from smartclaw.auth.tool_gate import get_tool_security_context


@dataclass(frozen=True)
class BusinessToolContext:
    tenant_id: str
    agent_id: str
    user_open_id: str
    session_id: str
    roles: tuple[str, ...]
    integration_env: dict[str, str]


def current_business_context(*, require: bool = True) -> BusinessToolContext:
    """Return the current tenant/user/agent context for business tools.

    Business tools should derive tenant boundaries from this helper instead of
    accepting a model-supplied ``tenant_id`` parameter.
    """
    ctx = get_tool_security_context()
    if ctx is None:
        if require:
            raise RuntimeError("缺少 ToolSecurityContext，无法确定当前 tenant/user")
        return BusinessToolContext(
            tenant_id="default",
            agent_id="",
            user_open_id="",
            session_id="",
            roles=("default",),
            integration_env={},
        )
    return BusinessToolContext(
        tenant_id=ctx.tenant_id or "default",
        agent_id=ctx.agent_id or "",
        user_open_id=ctx.feishu_open_id or "",
        session_id=ctx.session_id or "",
        roles=tuple(ctx.roles or ("default",)),
        integration_env=dict(ctx.integration_env or ()),
    )


def reject_model_supplied_tenant(parameters: dict[str, Any], *, allowed: bool = False) -> tuple[bool, str]:
    """Reject parameters that try to override tenant boundaries."""
    if allowed:
        return True, ""
    for key in ("tenant", "tenant_id", "org_id", "merchant_id"):
        if key in parameters and parameters.get(key):
            return False, f"业务工具禁止由模型传入 {key}；请使用当前 ToolSecurityContext.tenant_id"
    return True, ""


__all__ = ["BusinessToolContext", "current_business_context", "reject_model_supplied_tenant"]
