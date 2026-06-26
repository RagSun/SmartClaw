"""
在非 process_message / 非 _execute_tool 路径调用 ``tool_registry.execute`` 时，
绑定与 Runner 对齐的沙箱上下文与 exec 宿主策略上下文（与其它工具宿主策略一致）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

__all__ = ["runner_exec_context"]


@asynccontextmanager
async def runner_exec_context(runner: Any) -> AsyncIterator[None]:
    from smartclaw.agent.sandbox_context import reset_runner_sandbox, set_runner_sandbox
    from smartclaw.agent.tools.exec_context import (
        reset_agent_config_for_exec,
        reset_workspace_resolution_snap,
        set_agent_config_for_exec,
        set_workspace_resolution_snap,
    )

    sb_tok = None
    rb = getattr(runner, "sandbox_backend", None)
    ri = getattr(runner, "_sandbox_instance_id", None)
    if rb and ri:
        sb_tok = set_runner_sandbox(rb, ri)
    ws_snap = getattr(runner, "_workspace_resolution_snap", None)
    ws_tok = set_workspace_resolution_snap(ws_snap if isinstance(ws_snap, dict) else None)
    cfg_tok = set_agent_config_for_exec(getattr(runner, "_full_agent_config", None) or None)
    try:
        yield
    finally:
        reset_agent_config_for_exec(cfg_tok)
        reset_workspace_resolution_snap(ws_tok)
        if sb_tok is not None:
            reset_runner_sandbox(sb_tok)
