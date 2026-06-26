"""
exec 命令白名单（全局 config + Agent agent.json + 工作区 tools/SHELL_ALLOWLIST.txt 合并）。

层级说明（便于教学 / 排障）::

    ① Tool Policy（exec_policy）：危险正则 → DENY；elevated（如 docker）→ ELEVATED；
      内置 allowlist 首词 → ALLOW，否则 ASK（仅日志 ⚠️，不拦截）。
    ② 本模块 Shell 合并白名单：
      - 合并后为空 → 本层不启用（任意命令串在本层放行）。
      - 合并后非空 → 须至少命中一条规则。

匹配规则（合并后非空时）::

    - 单独一行 ``*`` 或 ``**``：表示「本层不做形状拦截」，交由 ① 处置风险。
      适合课堂 / 开发 Agent：仍需飞书角色 + ① 拦截高危与 Elevated。
    - 含 ``*`` / ``?`` / ``[`` 的条目：按 ``fnmatch`` 匹配「整行命令」或「首词」任一命中即可。
    - 其余条目：历史行为不变——整行前缀匹配，或首词精确相等。
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from smartclaw.config.loader import Config


def _normalize_patterns(raw: list[str]) -> list[str]:
    out: list[str] = []
    for line in raw:
        s = (line or "").strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def _load_file_patterns(path: str) -> list[str]:
    p = Path(os.path.expanduser(path.strip()))
    if not p.is_file():
        return []
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return []
    return _normalize_patterns(text.splitlines())


def _workspace_shell_file(workspace_root: Path | None) -> list[str]:
    if not workspace_root or not workspace_root.is_dir():
        return []
    f = workspace_root / "tools" / "SHELL_ALLOWLIST.txt"
    if not f.is_file():
        return []
    try:
        return _normalize_patterns(f.read_text(encoding="utf-8").splitlines())
    except OSError:
        return []


def collect_effective_patterns(
    *,
    cfg: Config | None,
    agent_config: dict[str, Any] | None,
    workspace_root: Path | None,
) -> list[str]:
    """汇总所有来源的白名单条目（去重保序）。"""
    merged: list[str] = []
    seen: set[str] = set()

    def add_many(items: list[str]) -> None:
        for x in items:
            if x not in seen:
                seen.add(x)
                merged.append(x)

    ex = getattr(cfg, "execution", None) if cfg else None
    if ex:
        add_many(_normalize_patterns(list(getattr(ex, "shell_allowlist", None) or [])))
        extra_path = (getattr(ex, "shell_allowlist_path", None) or "").strip()
        if extra_path:
            add_many(_load_file_patterns(extra_path))

    if agent_config:
        aj = agent_config.get("shell_allowlist")
        if isinstance(aj, list):
            add_many(_normalize_patterns([str(x) for x in aj]))
        elif isinstance(aj, str) and aj.strip():
            add_many(_normalize_patterns(aj.strip().splitlines()))

    if agent_config.get("shell_allowlist_include_workspace_file", True):
        add_many(_workspace_shell_file(workspace_root))

    return merged


def _command_first_token(command: str) -> str:
    s = command.strip()
    if not s:
        return ""
    return s.split()[0]


def _pattern_is_shell_unrestricted(pattern: str) -> bool:
    """``*`` / ``**`` 表示本层全放行（仍走 Tool Policy / 角色 / Agent tools）。"""
    p = pattern.strip()
    return p in ("*", "**")


def _matches_shell_pattern(cmd: str, token: str, pattern: str) -> bool:
    """
    单条白名单规则是否命中。

    - ``*`` / ``**``：非空命令即命中。
    - 含 glob 元字符：fnmatch 对整行 cmd 或首词 token 尝试匹配。
    - 否则：前缀或首词精确（兼容旧配置）。
    """
    p = pattern.strip()
    if not p:
        return False
    if _pattern_is_shell_unrestricted(p):
        return bool(cmd)
    if any(ch in p for ch in "*?["):
        try:
            return fnmatch.fnmatch(cmd, p) or fnmatch.fnmatch(token, p)
        except Exception:
            return cmd.startswith(p) or token == p
    return cmd.startswith(p) or token == p


_MAX_PREVIEW_ITEMS = 28
_MAX_PREVIEW_CHARS = 420


def _merge_contains_unrestricted_sentinel(patterns: list[str]) -> bool:
    return any(_pattern_is_shell_unrestricted(p) for p in patterns)


def _patterns_preview_for_log(patterns: list[str]) -> str:
    if not patterns:
        return "(none)"
    max_items = min(len(patterns), _MAX_PREVIEW_ITEMS)
    chunks = [
        p.replace("\n", "\\n").replace("\r", "\\r") for p in patterns[:max_items]
    ]
    s = " | ".join(chunks)
    if len(s) > _MAX_PREVIEW_CHARS:
        s = s[: _MAX_PREVIEW_CHARS - 1] + "…"
    if len(patterns) > max_items:
        s += f" …(+{len(patterns) - max_items} more)"
    return s


@dataclass(frozen=True)
class ShellAllowlistEval:
    """单次 Shell 合并白名单判定详情（日志 / 排障）。"""

    allowed: bool
    reason: str
    first_token: str
    pattern_count: int
    merge_contains_unrestricted_sentinel: bool
    matched_pattern: str | None
    patterns_preview: str


def evaluate_shell_allowlist(
    command: str,
    *,
    cfg: Config | None,
    agent_config: dict[str, Any] | None,
    workspace_root: Path | None,
) -> ShellAllowlistEval:
    """返回合并白名单判定详情；合并列表为空时本层不启用（任意命令放行）。"""
    patterns = collect_effective_patterns(
        cfg=cfg, agent_config=agent_config, workspace_root=workspace_root
    )
    unrestricted = _merge_contains_unrestricted_sentinel(patterns)
    preview = _patterns_preview_for_log(patterns)

    if not patterns:
        cmd0 = command.strip()
        token0 = _command_first_token(cmd0) if cmd0 else ""
        return ShellAllowlistEval(
            allowed=True,
            reason="",
            first_token=token0,
            pattern_count=0,
            merge_contains_unrestricted_sentinel=False,
            matched_pattern=None,
            patterns_preview="(merge_empty → shell_merge_layer_disabled)",
        )

    cmd = command.strip()
    if not cmd:
        return ShellAllowlistEval(
            allowed=False,
            reason="空命令",
            first_token="",
            pattern_count=len(patterns),
            merge_contains_unrestricted_sentinel=unrestricted,
            matched_pattern=None,
            patterns_preview=preview,
        )

    token = _command_first_token(cmd)
    for p in patterns:
        if _matches_shell_pattern(cmd, token, p):
            return ShellAllowlistEval(
                allowed=True,
                reason="",
                first_token=token,
                pattern_count=len(patterns),
                merge_contains_unrestricted_sentinel=unrestricted,
                matched_pattern=p,
                patterns_preview=preview,
            )

    long_reason = (
        "命令不在白名单（合并全局 execution.shell_allowlist、"
        "agent.json shell_allowlist、workspace/tools/SHELL_ALLOWLIST.txt）；"
        "支持前缀/首词、fnmatch（如 python*）、以及单独一行 * 表示本层全放行（仍走 Tool Policy）；"
        f" 首词={token!r}"
    )
    return ShellAllowlistEval(
        allowed=False,
        reason=long_reason,
        first_token=token,
        pattern_count=len(patterns),
        merge_contains_unrestricted_sentinel=unrestricted,
        matched_pattern=None,
        patterns_preview=preview,
    )


def is_shell_command_allowed(
    command: str,
    *,
    cfg: Config | None,
    agent_config: dict[str, Any] | None,
    workspace_root: Path | None,
) -> tuple[bool, str]:
    """
    Returns:
        (True, "") 放行
        (False, reason) 拒绝
    """
    ev = evaluate_shell_allowlist(
        command,
        cfg=cfg,
        agent_config=agent_config,
        workspace_root=workspace_root,
    )
    return ev.allowed, (ev.reason if not ev.allowed else "")


__all__ = [
    "ShellAllowlistEval",
    "collect_effective_patterns",
    "evaluate_shell_allowlist",
    "is_shell_command_allowed",
]
