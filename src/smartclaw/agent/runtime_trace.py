"""可选的详细运行时日志（与 SMARTCLAW_DEEPAGENTS_DEBUG 一致）。"""

from __future__ import annotations

import os


def is_deepagents_verbose() -> bool:
    """为 True 时：LangChain set_debug、create_deep_agent(debug)、以及沙箱每条 shell 的 trace。"""
    raw = (os.environ.get("SMARTCLAW_DEEPAGENTS_DEBUG") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


__all__ = ["is_deepagents_verbose"]
