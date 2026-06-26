"""
宿主命令（exec 子进程）策略：在「工具名 exec」门禁之后，统一评估 Tool Policy + Shell 白名单。

- 工具名权限：见 auth.tool_required_roles_any / tool_gate（按注册名如 exec、read_file）。
- 本模块：仅处理「已通过工具门」后的 shell 命令字符串。
- Shell 合并白名单语法见 shell_allowlist 模块：前缀/首词、fnmatch（python*）、单独 * 表示本层全放行（危险/Elevated 仍由 Tool Policy 处理）。
- 合并层诊断日志：环境变量 SMARTCLAW_HOSTCOMMAND_SHELL_LOG=0|false|no|off 可关闭（默认开启）。
- Tool Policy ASK 告警：SMARTCLAW_TOOL_POLICY_ASK_LOG=0|false|no|off 可关闭（默认开启；ASK 不拦截 exec，仅控制台提示）。
- Tool Policy DENY/ELEVATED：始终各打一行结构化日志（blocked_exec=true），便于与讲义 §8b 对照。
- Tool Policy ALLOW 一行：SMARTCLAW_TOOL_POLICY_ALLOW_LOG=1|true|yes|on 开启（默认关闭；命中内置粗表 ALLOW 时打印）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from smartclaw.console import info, warning
from smartclaw.exec_policy import PolicyAction, PolicyResult, check_command

if TYPE_CHECKING:
    from smartclaw.config.loader import Config


def _host_command_shell_merge_log_enabled() -> bool:
    v = os.getenv("SMARTCLAW_HOSTCOMMAND_SHELL_LOG", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _tool_policy_ask_log_enabled() -> bool:
    """PolicyAction.ASK 仅告警；生产可关日志降噪（判定逻辑不变）。"""
    v = os.getenv("SMARTCLAW_TOOL_POLICY_ASK_LOG", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _tool_policy_allow_log_enabled() -> bool:
    """命中内置粗表 ALLOW 时可选打印一行（默认关，防刷屏）。"""
    v = os.getenv("SMARTCLAW_TOOL_POLICY_ALLOW_LOG", "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def _host_command_cmd_preview(command: str, limit: int = 200) -> str:
    return command.strip().replace("\n", "\\n")[:limit]


@dataclass(frozen=True)
class HostCommandVerdict:
    """宿主命令是否可进入 subprocess（不含脚本存在性预检等执行前检查）。"""

    allowed: bool
    """是否通过 tool_policy + shell_allowlist。"""
    layer: str
    """拒绝或判定语义分层：tool_policy | shell_allowlist | host_command。"""
    rule_id: str
    """稳定机读标识，如 tool_policy:deny、shell_allowlist:no_match、host_command:ok。"""
    message: str
    """给人看的说明。"""
    policy_result: PolicyResult | None
    """最近一次 Tool Policy 结果（通过时用于回显 / 日志）。"""


def resolve_agent_context_for_host_command(cfg: "Config") -> tuple[dict[str, Any], Path]:
    """与 ExecTool 一致：exec 上下文快照 → agent.json → workspace 根目录。"""
    from smartclaw.agent.manager import AgentManager
    from smartclaw.agent.tools.exec_context import (
        get_agent_config_for_exec,
        get_workspace_resolution_snap,
    )
    from smartclaw.agent.workspace import resolve_agent_workspace_dir
    from smartclaw.auth.tool_gate import get_tool_security_context
    from smartclaw.tenant import normalize_tenant_id

    snap_merged = None
    try:
        ws_snap = get_workspace_resolution_snap()
        if ws_snap is not None:
            snap_merged = dict(ws_snap)
        else:
            ac_inline = get_agent_config_for_exec()
            if ac_inline:
                snap_merged = dict(ac_inline)
    except Exception:
        snap_merged = None

    if snap_merged is not None:
        ac = snap_merged
    else:
        ac = get_agent_config_for_exec()
        if ac is None:
            ac = {}
    if not ac:
        tctx = get_tool_security_context()
        if tctx and getattr(tctx, "agent_id", None):
            ac = AgentManager()._read_config(
                tctx.agent_id,
                tenant_id=getattr(tctx, "tenant_id", "default"),
            ) or {}
        else:
            ac = {}
    logical = str(ac.get("name") or "").strip()
    if not logical:
        tctx = get_tool_security_context()
        if tctx and getattr(tctx, "agent_id", None):
            logical = str(tctx.agent_id)
    if not logical:
        logical = "default"
    tctx = get_tool_security_context()
    ctx_tid = getattr(tctx, "tenant_id", None) if tctx else None
    if ctx_tid is not None and str(ctx_tid).strip():
        tenant_kw = normalize_tenant_id(str(ctx_tid).strip())
    else:
        ac_tid = ac.get("tenant_id")
        if ac_tid is not None and str(ac_tid).strip():
            tenant_kw = normalize_tenant_id(str(ac_tid).strip())
        else:
            tenant_kw = normalize_tenant_id(None)
    ws_root = resolve_agent_workspace_dir(logical, ac, cfg, tenant_id=tenant_kw)
    return ac, ws_root


def evaluate_host_command(
    command: str,
    *,
    cfg: "Config",
    agent_config: dict[str, Any] | None = None,
    workspace_root: Path | None = None,
) -> HostCommandVerdict:
    """
    评估宿主 shell 命令（Tool Policy → 合并 Shell 白名单）。
    若未传入 agent_config/workspace_root，则从当前 exec 上下文解析。
    """
    from smartclaw.agent.shell_allowlist import evaluate_shell_allowlist

    ac_resolved, ws_resolved = resolve_agent_context_for_host_command(cfg)
    if agent_config is None:
        agent_config = ac_resolved
    if workspace_root is None:
        workspace_root = ws_resolved

    pr = check_command(command)
    cmd_pv = _host_command_cmd_preview(command)

    if pr.action == PolicyAction.DENY:
        warning(
            "[Tool Policy] DENY | PolicyAction=DENY blocked_exec=true "
            f"reason={pr.reason!r} sh_first={pr.tool!r} cmd_preview={cmd_pv!r}"
        )
        return HostCommandVerdict(
            allowed=False,
            layer="tool_policy",
            rule_id="tool_policy:deny",
            message=pr.reason,
            policy_result=pr,
        )
    if pr.action == PolicyAction.ELEVATED:
        warning(
            "[Tool Policy] ELEVATED | PolicyAction=ELEVATED blocked_exec=true "
            f"reason={pr.reason!r} sh_first={pr.tool!r} cmd_preview={cmd_pv!r}"
        )
        return HostCommandVerdict(
            allowed=False,
            layer="tool_policy",
            rule_id="tool_policy:elevated",
            message=pr.reason,
            policy_result=pr,
        )
    if pr.action == PolicyAction.ALLOW and _tool_policy_allow_log_enabled():
        info(
            "[Tool Policy] ALLOW | PolicyAction=ALLOW blocked_exec=false "
            f"reason={pr.reason!r} sh_first={pr.tool!r} cmd_preview={cmd_pv!r}"
        )
    if pr.action == PolicyAction.ASK and _tool_policy_ask_log_enabled():
        warning(
            "[Tool Policy] ASK | PolicyAction=ASK blocked_exec=false "
            f"reason={pr.reason!r} sh_first={pr.tool!r} cmd_preview={cmd_pv!r} | "
            "note=下一步仍校验Shell合并白名单（execution.shell_allowlist / agent.json / "
            "workspace/tools/SHELL_ALLOWLIST.txt）"
        )

    sl_ev = evaluate_shell_allowlist(
        command,
        cfg=cfg,
        agent_config=agent_config,
        workspace_root=workspace_root,
    )
    if _host_command_shell_merge_log_enabled():
        sentinel_note = (
            "merge_has_*_sentinel=yes（本层形状近乎全放行，仍走 Tool Policy）"
            if sl_ev.merge_contains_unrestricted_sentinel
            else "merge_has_*_sentinel=no"
        )
        matched = repr(sl_ev.matched_pattern) if sl_ev.matched_pattern else "None"
        line = (
            "[HostCommand] Shell合并白名单 | "
            f"n_patterns={sl_ev.pattern_count} {sentinel_note} "
            f"sh_first={sl_ev.first_token!r} matched_rule={matched} "
            f"allowed={sl_ev.allowed} preview_rules={sl_ev.patterns_preview} "
            f"cmd_preview={cmd_pv!r}"
        )
        if sl_ev.allowed:
            info(line)
        else:
            warning(line)

    if not sl_ev.allowed:
        return HostCommandVerdict(
            allowed=False,
            layer="shell_allowlist",
            rule_id="shell_allowlist:no_match",
            message=sl_ev.reason,
            policy_result=pr,
        )

    return HostCommandVerdict(
        allowed=True,
        layer="host_command",
        rule_id="host_command:ok",
        message="",
        policy_result=pr,
    )


def build_exec_tool_denial_dict(
    command: str, verdict: HostCommandVerdict
) -> dict[str, Any]:
    """与 ExecTool 拒绝分支一致，供 exec_handler / ExecTool 复用。"""
    policy_result = verdict.policy_result
    if verdict.rule_id == "tool_policy:elevated":
        prefix = "[需要 Elevated 权限]"
        tail = (
            f"\n命令: {command[:100]}\n\n"
            "如需执行，请联系管理员配置 Elevated 权限。"
        )
    elif verdict.layer == "tool_policy":
        prefix = "[策略拒绝]"
        tail = f"\n命令: {command[:100]}"
    else:
        prefix = "[Shell 白名单]"
        tail = f"\n命令: {command[:100]}"
    out: dict[str, Any] = {
        "success": False,
        "error": f"{prefix} {verdict.message}{tail}",
        "output": "",
        "exit_code": -1,
        "policy": str(policy_result) if policy_result else verdict.rule_id,
        "host_command": {
            "layer": verdict.layer,
            "rule_id": verdict.rule_id,
        },
    }
    if verdict.rule_id == "tool_policy:elevated":
        out["requires_elevated"] = True
    return out


def format_exec_handler_denial_string(command: str, verdict: HostCommandVerdict) -> str:
    """Registry exec 顶层返回给用户/模型的单行错误（与 exec_handler 拼装习惯一致）。"""
    d = build_exec_tool_denial_dict(command, verdict)
    return f"错误: {d['error']}\n{d.get('output', '')}"


__all__ = [
    "HostCommandVerdict",
    "build_exec_tool_denial_dict",
    "evaluate_host_command",
    "format_exec_handler_denial_string",
    "resolve_agent_context_for_host_command",
]
