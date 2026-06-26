"""
DeepAgents 内置 ``execute`` 与 Registry ``exec`` 归为同一 Shell 能力，门禁对齐。

在未走 ToolRegistry.execute 时仍应用：
- evaluate_host_command（Tool Policy + 合并 shell_allowlist）
- agent.json 对 exec / execute 的 allow / deny / enforce
- auth 对 exec 与 execute 的角色要求（两键若在配置中存在则各自须能通过）

说明：ToolRegistry.sandbox_* 仅作遗留回退；Runner 处理消息时优先使用 ``sandbox_context`` ContextVar。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from deepagents.backends.protocol import ExecuteResponse

from smartclaw.agent.host_command_gate import evaluate_host_command
from smartclaw.agent.manager import AgentManager
from smartclaw.agent.tools.registry import ToolRegistry
from smartclaw.audit.logger import audit_tool
from smartclaw.auth.tool_gate import (
    ToolSecurityContext,
    check_tool_allowed,
    check_shell_capability_allowed,
    get_tool_security_context,
)
from smartclaw.config.loader import get_config

if TYPE_CHECKING:
    from smartclaw.config.loader import Config

# 与 README/系统提示一致：框架「execute」与登记「exec」视为同一类 Shell 能力
SHELL_CAPABILITY_TOOL_NAMES = frozenset({"exec", "execute"})
FILE_WRITE_CAPABILITY_TOOL_NAMES = frozenset({"write_file", "edit_file"})


def _blocked_by_agent_shell_policy(agent_cfg: dict[str, Any]) -> str:
    allowed, denied, enforce = ToolRegistry._agent_tool_policy(agent_cfg)
    if denied & SHELL_CAPABILITY_TOOL_NAMES:
        denied_hits = sorted(denied & SHELL_CAPABILITY_TOOL_NAMES)
        return (
            f"Agent 策略禁止 Shell 能力（denied_tools 命中: {denied_hits!r}）；"
            "DeepAgents execute 与 Registry exec 同属此类。"
        )
    if enforce:
        if not (allowed & SHELL_CAPABILITY_TOOL_NAMES):
            return (
                "enforce_allowed_tools 已启用：须在 allowed_tools 中显式包含 "
                "`exec` 或 `execute` 之一才允许 Shell（含 DeepAgents execute）。"
            )
    return ""


def _blocked_by_agent_tool_policy(agent_cfg: dict[str, Any], tool_name: str) -> str:
    allowed, denied, enforce = ToolRegistry._agent_tool_policy(agent_cfg)
    if tool_name in denied:
        return f"Agent 策略禁止 DeepAgents 内置工具: {tool_name}"
    if enforce and tool_name not in allowed:
        return (
            "enforce_allowed_tools 已启用：须在 allowed_tools 中显式包含 "
            f"`{tool_name}` 才允许 DeepAgents 内置文件写入。"
        )
    return ""


def deepagents_shell_blocked_response(message: str) -> ExecuteResponse:
    return ExecuteResponse(output=message, exit_code=1, truncated=False)


def _normalize_deepagents_command(command: str | None) -> str:
    return (command or "").strip()


def _gate_deepagents_nonempty_command(command_stripped: str) -> ExecuteResponse | None:
    if not command_stripped:
        return deepagents_shell_blocked_response("Error: Command must be a non-empty string.")
    return None


def _gate_deepagents_evaluate_host_command(
    command_stripped: str,
    *,
    cfg: "Config",
    workspace_root: Path | None,
) -> ExecuteResponse | None:
    verdict = evaluate_host_command(
        command_stripped, cfg=cfg, workspace_root=workspace_root
    )
    if not verdict.allowed:
        return deepagents_shell_blocked_response(
            f"宿主命令门禁拒绝 DeepAgents execute：{verdict.message} "
            f"（layer={verdict.layer}, rule={verdict.rule_id}）"
        )
    return None


def _read_deepagents_agent_cfg(ctx: ToolSecurityContext) -> dict[str, Any]:
    try:
        return (
            AgentManager()._read_config(ctx.agent_id, tenant_id=ctx.tenant_id) or {}
        )
    except Exception:
        return {}


def _audit_deepagents_gate_block(
    ctx: ToolSecurityContext, *, prefix: str, detail: str, rule_key: str
) -> None:
    audit_tool(
        tenant_id=ctx.tenant_id,
        user_open_id=ctx.feishu_open_id,
        agent_id=ctx.agent_id,
        tool_name="execute",
        success=False,
        error=f"{prefix}:{detail}",
        metadata={"rule": rule_key, "surface": "deepagents_execute"},
    )


def _audit_deepagents_file_write_block(
    ctx: ToolSecurityContext, *, tool_name: str, detail: str, rule_key: str
) -> None:
    audit_tool(
        tenant_id=ctx.tenant_id,
        user_open_id=ctx.feishu_open_id,
        agent_id=ctx.agent_id,
        tool_name=tool_name,
        success=False,
        error=f"deepagents_file_write_gate:{detail}",
        metadata={"rule": rule_key, "surface": "deepagents_file_write"},
    )


def gate_deepagents_file_write(tool_name: str, *, cfg: "Config | None" = None) -> str | None:
    """
    Gate DeepAgents built-in write_file/edit_file, which do not pass through ToolRegistry.

    Returns a human-readable denial reason, or None when allowed.
    """
    if tool_name not in FILE_WRITE_CAPABILITY_TOOL_NAMES:
        return None

    ctx = get_tool_security_context()
    if not ctx:
        return None

    cfg = cfg or get_config()
    agent_cfg = _read_deepagents_agent_cfg(ctx)
    pol = _blocked_by_agent_tool_policy(agent_cfg, tool_name)
    if pol:
        _audit_deepagents_file_write_block(
            ctx,
            tool_name=tool_name,
            detail=f"policy:{pol}",
            rule_key="agent_tool_policy",
        )
        return pol

    ok_roles, role_reason = check_tool_allowed(tool_name, ctx, cfg)
    if not ok_roles:
        _audit_deepagents_file_write_block(
            ctx,
            tool_name=tool_name,
            detail=f"roles:{role_reason}",
            rule_key="tool_roles",
        )
        return role_reason
    return None


def gate_deepagents_execute(
    command: str,
    *,
    workspace_root: Path | None,
    cfg: "Config | None" = None,
) -> ExecuteResponse | None:
    """
    DeepAgents Backend.execute / aexecute 首段门禁（固定顺序，便于对照文档）。

    1. 非空命令
    2. ``evaluate_host_command``（工具策略 + shell_allowlist 合并域）
    3. 无 ``ToolSecurityContext`` → 放行（宿主侧已拦；与 ToolRegistry.execute 对齐）
    4. agent.json → ``_blocked_by_agent_shell_policy``
    5. ``check_shell_capability_allowed``

    返回 ExecuteResponse → 应立即返回给上层，不再 subprocess/沙箱。
    返回 None → 放行（由后端继续）。
    """
    cfg = cfg or get_config()
    cmd_stripped = _normalize_deepagents_command(command)

    rej = _gate_deepagents_nonempty_command(cmd_stripped)
    if rej is not None:
        return rej

    rej = _gate_deepagents_evaluate_host_command(
        cmd_stripped, cfg=cfg, workspace_root=workspace_root
    )
    if rej is not None:
        return rej

    ctx = get_tool_security_context()
    # 与 ToolRegistry.execute 一致：无会话上下文时不做租户/Agent 策略，仅宿主命令门禁已生效。
    if not ctx:
        return None

    agent_cfg = _read_deepagents_agent_cfg(ctx)
    pol = _blocked_by_agent_shell_policy(agent_cfg)
    if pol:
        _audit_deepagents_gate_block(
            ctx, prefix="deepagents_shell_gate", detail=f"policy:{pol}", rule_key="agent_tool_policy"
        )
        return deepagents_shell_blocked_response(pol)

    ok_roles, role_reason = check_shell_capability_allowed(ctx, cfg)
    if not ok_roles:
        _audit_deepagents_gate_block(
            ctx,
            prefix="deepagents_shell_gate",
            detail=f"roles:{role_reason}",
            rule_key="shell_capability_roles",
        )
        return deepagents_shell_blocked_response(role_reason)

    return None


__all__ = [
    "FILE_WRITE_CAPABILITY_TOOL_NAMES",
    "SHELL_CAPABILITY_TOOL_NAMES",
    "deepagents_shell_blocked_response",
    "gate_deepagents_file_write",
    "gate_deepagents_execute",
]
