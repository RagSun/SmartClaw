"""Skill runtime inspection and safe full SKILL.md loading."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from smartclaw.auth.tool_gate import get_tool_security_context
from smartclaw.config.loader import get_config
from smartclaw.skills.loader import load_workspace_skill_entries
from smartclaw.skills.status import build_workspace_skill_status


def _workspace_dir(workspace_dir: str | None = None) -> str:
    """Resolve the current tenant-aware workspace directory."""
    if workspace_dir and workspace_dir.strip():
        return str(Path(workspace_dir).expanduser().resolve())
    from smartclaw.agent.workspace import resolve_agent_workspace_dir, resolve_agent_workspace_for_tools

    wr = resolve_agent_workspace_for_tools()
    if wr is not None:
        return str(wr.resolve())
    ctx = get_tool_security_context()
    agent_id = (ctx.agent_id if ctx else "") or "default"
    tenant_id = (ctx.tenant_id if ctx else "") or "default"
    return str(
        resolve_agent_workspace_dir(
            agent_id,
            {"tenant_id": tenant_id},
            get_config(),
            tenant_id=tenant_id,
        )
    )


def _find_skill(workspace_dir: str, skill_key: str) -> Any:
    target = (skill_key or "").strip()
    for skill in load_workspace_skill_entries(workspace_dir, config=get_config()):
        if target in {skill.name, skill.metadata.skill_key}:
            return skill
    return None


def skill_audit_handler(
    action: str = "status",
    skill_key: str | None = None,
    workspace_dir: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """
    Inspect skills.

    action:
      - status/scan: list skills with version/owner/risk/test/security/tenant findings.
      - read_skill: load full SKILL.md on demand.
    """
    root = _workspace_dir(workspace_dir)
    act = (action or "status").strip().lower()

    if act in {"status", "scan", "list"}:
        return {
            "success": True,
            "status": build_workspace_skill_status(
                root,
                config=get_config(),
                tenant_id=tenant_id,
            ),
        }

    if act == "read_skill":
        if not skill_key:
            return {"success": False, "error": "read_skill 需要 skill_key"}
        skill = _find_skill(root, skill_key)
        if not skill:
            return {"success": False, "error": f"未找到 skill: {skill_key}"}
        p = Path(skill.skill_md_path).resolve()
        base = Path(skill.base_dir).resolve()
        try:
            p.relative_to(base)
        except ValueError:
            return {"success": False, "error": "SKILL.md 路径越界，拒绝读取"}
        limits = get_config().skills.limits
        if p.stat().st_size > int(limits.max_skill_file_bytes):
            return {"success": False, "error": "SKILL.md 超过 max_skill_file_bytes"}
        content = p.read_text(encoding="utf-8", errors="replace")
        return {
            "success": True,
            "skill_key": skill.metadata.skill_key or skill.name,
            "path": str(p),
            "metadata": {
                "version": skill.metadata.version,
                "owner": skill.metadata.owner,
                "reviewer": skill.metadata.reviewer,
                "risk_level": skill.metadata.risk_level,
                "test_status": skill.metadata.test_status,
            },
            "content": content,
        }

    return {"success": False, "error": f"未知 action: {action}"}


SKILL_AUDIT_TOOL_DEFINITION = {
    "name": "skill_audit",
    "description": "检查 skill 运行时状态、安全扫描结果，并按需读取完整 SKILL.md。",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "status/scan/list/read_skill，默认 status",
            },
            "skill_key": {"type": "string", "description": "read_skill 时的 skill_key"},
            "workspace_dir": {"type": "string", "description": "可选工作区路径"},
            "tenant_id": {"type": "string", "description": "可选租户 ID，默认当前上下文"},
        },
        "required": [],
    },
}


__all__ = ["SKILL_AUDIT_TOOL_DEFINITION", "skill_audit_handler"]
