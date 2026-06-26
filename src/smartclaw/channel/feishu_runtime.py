"""Shared Feishu runtime helpers for session ids and local attachment paths."""

from __future__ import annotations

import re
from pathlib import Path

import smartclaw.paths as paths


_SAFE_PART_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_part(value: str, fallback: str) -> str:
    cleaned = _SAFE_PART_RE.sub("_", (value or "").strip()).strip("._")
    return cleaned or fallback


def build_feishu_session_id(
    *,
    user_open_id: str,
    app_id: str,
    chat_id: str = "",
    is_group: bool = False,
) -> str:
    """Return a stable session id shared by Feishu single and multi process modes."""
    anchor = chat_id if is_group and chat_id else user_open_id
    return f"{_safe_part(anchor, 'unknown_user')}_{_safe_part(app_id, 'unknown_app')}"


def feishu_reply_chat_id(*, user_open_id: str, chat_id: str = "", is_group: bool = False) -> str:
    """Return the Feishu target used for replies."""
    if is_group and chat_id:
        return chat_id
    return user_open_id or chat_id


def feishu_download_dir(agent_name: str, tenant_id: str = "default") -> Path:
    """Directory for Feishu resource downloads under the unified runtime temp root."""
    return (
        paths.TEMP_DIR
        / "feishu"
        / _safe_part(tenant_id, "default")
        / _safe_part(agent_name, "agent")
        / "downloads"
    )
