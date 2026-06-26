"""轻量审计：追加写入 ``<audit_dir>/*.jsonl``。

目录优先级（向后兼容，旧路径仍为最后兜底）:

1. ``SMARTCLAW_AUDIT_DIR`` 环境变量（运维/容器化场景显式注入）
2. ``SMARTCLAW_HOME/audit``（与 ``smartclaw.paths.INSTALL_ROOT`` 对齐，
   非 root 安装 / 自定义安装根都能正确写入）
3. ``~/.smartclaw/audit``（旧默认行为）
4. ``$TMPDIR/smartclaw-audit-<uid>``（前 3 项均不可写时的兜底，**永不抛 PermissionError**）
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smartclaw.config.loader import get_config
from smartclaw.logging_utils import redact_text


def _try_make_writable(d: Path) -> Path | None:
    """目录可成功 ``mkdir -p`` 且可写入时返回 ``d``，否则返回 ``None``。"""
    try:
        d.mkdir(parents=True, exist_ok=True)
    except (OSError, PermissionError):
        return None
    if not os.access(str(d), os.W_OK):
        return None
    return d


def _audit_dir() -> Path:
    # 1) 显式环境变量
    env_raw = (os.environ.get("SMARTCLAW_AUDIT_DIR") or "").strip()
    if env_raw:
        cand = Path(env_raw).expanduser()
        ok = _try_make_writable(cand)
        if ok is not None:
            return ok
    # 2) paths.py 统一安装根
    try:
        from smartclaw.paths import INSTALL_ROOT as _IR
        ok = _try_make_writable(Path(_IR) / "audit")
        if ok is not None:
            return ok
    except Exception:
        pass
    # 3) 旧默认（与历史完全一致）
    ok = _try_make_writable(Path.home() / ".smartclaw" / "audit")
    if ok is not None:
        return ok
    # 4) 最后兜底：tmp，永不抛
    fallback = Path(tempfile.gettempdir()) / f"smartclaw-audit-{os.getuid() if hasattr(os, 'getuid') else 'na'}"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def append_audit(stream: str, payload: dict[str, Any]) -> None:
    try:
        cfg = get_config()
        if not getattr(cfg.auth, "audit_jsonl_enabled", True):
            return
    except Exception:
        pass
    path = _audit_dir() / f"{stream}.jsonl"
    line = {"ts": datetime.now(timezone.utc).isoformat(), **payload}
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(redact_text(json.dumps(line, ensure_ascii=False)) + "\n")
    except (OSError, PermissionError):
        # 审计本应是 best-effort 旁路：磁盘满 / 只读 fs / 权限突变都不应阻断调用方主流程。
        # 旧实现会直接抛 PermissionError 导致 audit_tool() 把上游 tool 调用一并搞挂。
        # 此处 swallow，保留 console 一条告警以便运维感知。
        try:
            from smartclaw.console import warning as _warn
            _warn(f"[audit] 写入失败已忽略: {path}")
        except Exception:
            pass


def feishu_inbound(
    *,
    transport: str,
    tenant_id: str,
    feishu_app_id: str,
    agent_name: str,
    user_open_id: str,
    chat_id: str,
    is_group: bool,
    action: str,
    detail: str = "",
    roles: list[str] | None = None,
) -> None:
    append_audit(
        "feishu-inbound",
        {
            "transport": transport,
            "tenant_id": tenant_id,
            "feishu_app_id": feishu_app_id,
            "agent_name": agent_name,
            "user_open_id": user_open_id,
            "chat_id": chat_id,
            "is_group": is_group,
            "action": action,
            "detail": detail[:500] if detail else "",
            "roles": roles or [],
        },
    )


def audit_tool(
    *,
    tenant_id: str,
    user_open_id: str,
    agent_id: str,
    tool_name: str,
    success: bool,
    error: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Append a single tool invocation audit record.

    The optional metadata snapshot is intentionally small and non-secret. It
    lets operators answer "which risk class and tenant scope was invoked" from
    the audit log without reloading the current registry, which may have
    changed after the invocation.
    """
    append_audit(
        "tool-invoke",
        {
            "tenant_id": tenant_id,
            "user_open_id": user_open_id,
            "agent_id": agent_id,
            "tool_name": tool_name,
            "success": success,
            "error": error[:500] if error else "",
            "metadata": metadata or {},
        },
    )
