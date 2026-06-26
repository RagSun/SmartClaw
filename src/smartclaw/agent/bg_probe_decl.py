"""
声明式后台服务探测配置（框架无关）。

来源（后者覆盖前者）：
1. agent.json 中 ``execution.bg_probe``，或顶层 ``bg_probe``（对象）
2. 工作区 ``.smartclaw/bg_probe.json``

字段（均在 ``bg_probe`` 对象 / JSON 内）：
- ``after_smoke`` (str)：后台启动后轮询结束前在**工作区根 cwd**执行的 shell 命令；
  可由任意框架实现自己的检查（grpcurl、kubectl、脚本等）。
- ``after_smoke_timeout_sec`` (float，默认 30)：smoke 命令超时。
- ``tcp_probe`` (bool，默认 True)：若为 false，跳过内置 TCP/HTTP 轮询，
  仅依赖 ``after_smoke``（或未配置时等价于跳过自定义 smoke 且无 TCP）。

执行 ``after_smoke`` 时会注入环境变量：
- ``SMARTCLAW_BG_INFER_PORT``：从启动命令推断的监听端口数字串，推断失败为空串。
- ``SMARTCLAW_BG_START_CMD``：本次后台启动的命令行（截断约 8190 字节）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class BgProbeDecl:
    """合并后的探测声明（仅用于运行时读；不做 JSON Schema 校验）。"""

    after_smoke: str
    after_smoke_timeout_sec: float
    tcp_probe: bool


def default_bg_probe_decl() -> BgProbeDecl:
    return BgProbeDecl(after_smoke="", after_smoke_timeout_sec=30.0, tcp_probe=True)


def _normalize_decl_dict(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return raw


def _extract_agent_bg_probe(agent_cfg: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(agent_cfg, Mapping):
        return {}
    exe = agent_cfg.get("execution")
    if isinstance(exe, Mapping):
        bp = exe.get("bg_probe")
        if isinstance(bp, Mapping):
            return dict(bp)
    top = agent_cfg.get("bg_probe")
    return dict(top) if isinstance(top, Mapping) else {}


def _read_workspace_probe_file(workspace_root: Path) -> dict[str, Any]:
    p = workspace_root / ".smartclaw" / "bg_probe.json"
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return _normalize_decl_dict(data)


def _merge_shallow(agent: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(agent)
    merged.update(override)
    return merged


def resolve_bg_probe_decl(
    *,
    agent_cfg: Mapping[str, Any] | None,
    workspace_root: Path,
) -> BgProbeDecl:
    """
    合并顺序：agent 默认 → ``.smartclaw/bg_probe.json`` 覆盖同名字段。
    """
    defaults = default_bg_probe_decl()
    merged = _merge_shallow(_extract_agent_bg_probe(agent_cfg), _read_workspace_probe_file(workspace_root))

    smoke = merged.get("after_smoke") or merged.get("smoke")
    smoke_s = smoke.strip() if isinstance(smoke, str) else ""

    raw_to = merged.get("after_smoke_timeout_sec")
    if raw_to is None:
        raw_to = merged.get("smoke_timeout_sec")
    timeout = defaults.after_smoke_timeout_sec
    if raw_to is not None:
        try:
            timeout = float(raw_to)
        except (TypeError, ValueError):
            timeout = defaults.after_smoke_timeout_sec
    timeout = max(1.0, min(timeout, 600.0))

    tcp = merged.get("tcp_probe")
    tcp_b = defaults.tcp_probe
    if isinstance(tcp, bool):
        tcp_b = tcp
    elif tcp is not None:
        ss = str(tcp).strip().lower()
        if ss in {"0", "false", "no", "off"}:
            tcp_b = False
        elif ss in {"1", "true", "yes", "on"}:
            tcp_b = True

    return BgProbeDecl(
        after_smoke=smoke_s,
        after_smoke_timeout_sec=timeout,
        tcp_probe=tcp_b,
    )


__all__ = [
    "BgProbeDecl",
    "default_bg_probe_decl",
    "resolve_bg_probe_decl",
]
