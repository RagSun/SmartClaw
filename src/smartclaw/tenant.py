"""Tenant namespace helpers.

Tenant IDs are the top-level isolation boundary for data, workspace, runtime
state and configuration. These helpers deliberately keep ``default`` compatible
with the historical single-tenant layout while placing non-default tenants under
their own namespace.
"""

from __future__ import annotations

import re
from pathlib import Path


DEFAULT_TENANT_ID = "default"


def normalize_tenant_id(tenant_id: str | None) -> str:
    """Return a safe tenant identifier, falling back to ``default``."""
    raw = (tenant_id or "").strip() or DEFAULT_TENANT_ID
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("._-")
    return slug or DEFAULT_TENANT_ID


def normalize_agent_name(agent_name: str | None) -> str:
    """Return a safe agent name for path construction."""
    raw = (agent_name or "").strip() or "default"
    slug = re.sub(r"[^A-Za-z0-9_]+", "_", raw).strip("_")
    return slug or "default"


def tenant_agent_key(agent_name: str, tenant_id: str | None = None) -> str:
    """Return a stable logical key for logs and maps."""
    tenant = normalize_tenant_id(tenant_id)
    agent = normalize_agent_name(agent_name)
    if tenant == DEFAULT_TENANT_ID:
        return agent
    return f"{tenant}/{agent}"


def tenant_scoped_child(base: Path, name: str, tenant_id: str | None = None) -> Path:
    """Return ``base/name`` for default tenant, otherwise ``base/tenant/name``."""
    tenant = normalize_tenant_id(tenant_id)
    child = normalize_agent_name(name)
    if tenant == DEFAULT_TENANT_ID:
        return base / child
    return base / tenant / child


__all__ = [
    "DEFAULT_TENANT_ID",
    "normalize_agent_name",
    "normalize_tenant_id",
    "tenant_agent_key",
    "tenant_scoped_child",
]
