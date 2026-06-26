"""Central metadata helpers for built-in tools.

Tool metadata is kept separate from JSON Schema so the same tool definition can
serve both the model-facing function interface and the operator-facing control
plane. The fields are intentionally small, stable and non-secret so they can be
written into audit logs.
"""

from __future__ import annotations

from typing import Any


DEFAULT_TOOL_METADATA: dict[str, Any] = {
    "owner": "smartclaw-core",
    "version": "0.1.0",
    "risk_level": "medium",
    "test_status": "smoke",
    "tenant_scope": "tenant",
    "audit_required": True,
    "lifecycle": "runtime",
}


BUILTIN_TOOL_METADATA: dict[str, dict[str, Any]] = {
    "exec": {"risk_level": "high", "tenant_scope": "workspace", "requires_confirmation": True},
    "read_file": {"risk_level": "low", "tenant_scope": "workspace"},
    "write_file": {"risk_level": "high", "tenant_scope": "workspace", "requires_confirmation": True},
    "write_todos": {"risk_level": "low"},
    "agent_create": {"risk_level": "high", "tenant_scope": "tenant_admin", "requires_confirmation": True},
    "agent_update_feishu": {"risk_level": "high", "tenant_scope": "tenant_admin", "requires_confirmation": True},
    "agent_status": {"risk_level": "low", "tenant_scope": "tenant_admin"},
    "reload_workspace_tools": {"risk_level": "high", "tenant_scope": "workspace", "requires_confirmation": True},
    "workspace_tool_status": {"risk_level": "medium", "tenant_scope": "workspace"},
    "upload_and_send_image": {"risk_level": "medium", "tenant_scope": "channel"},
    "create_feishu_doc": {"risk_level": "medium", "tenant_scope": "channel"},
    "integration_http_request": {"risk_level": "high", "tenant_scope": "tenant", "requires_confirmation": True},
    "background_task": {"risk_level": "high", "tenant_scope": "agent_process", "requires_confirmation": True},
    "spawn_subagent": {"risk_level": "high", "tenant_scope": "tenant_agent_session", "requires_confirmation": True},
    "subagent_status": {"risk_level": "low", "tenant_scope": "tenant_agent_session"},
    "subagent_cancel": {"risk_level": "medium", "tenant_scope": "tenant_agent_session"},
    "memory_search": {"risk_level": "low", "tenant_scope": "tenant_user_session"},
    "memory_get": {"risk_level": "low", "tenant_scope": "tenant_user_session"},
    "memory_write": {"risk_level": "medium", "tenant_scope": "tenant_user"},
    "skill_audit": {"risk_level": "medium", "tenant_scope": "workspace"},
    "tool_audit": {"risk_level": "low", "tenant_scope": "tenant"},
    "docker_project": {"risk_level": "high", "tenant_scope": "workspace", "requires_confirmation": True},
    "docker_snapshot": {"risk_level": "medium", "tenant_scope": "workspace"},
    "docker_monitor": {"risk_level": "medium", "tenant_scope": "workspace"},
}


def metadata_for_tool(name: str, **overrides: Any) -> dict[str, Any]:
    """Build normalized metadata for a tool registration.

    Parameters:
        name: Registered tool name.
        overrides: Optional field overrides for special cases.

    Returns:
        A merged metadata dictionary suitable for registry storage and audit.
    """
    data = dict(DEFAULT_TOOL_METADATA)
    data.update(BUILTIN_TOOL_METADATA.get(name, {}))
    data.update({k: v for k, v in overrides.items() if v is not None})
    return data


__all__ = ["BUILTIN_TOOL_METADATA", "DEFAULT_TOOL_METADATA", "metadata_for_tool"]
