"""
当前 asyncio 任务绑定的沙箱（backend + instance_id）。

多 Agent 同进程时，禁止依赖 ToolRegistry 全局 sandbox 指针（会被后启动的 Runner 覆盖）；
在 process_message（及同源工具链）内写入本 ContextVar，exec 等平台路径优先读本上下文。
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    pass

_active: ContextVar[Optional[tuple[Any, str]]] = ContextVar(
    "smartclaw_runner_sandbox", default=None
)


def get_runner_sandbox() -> Optional[tuple[Any, str]]:
    """Returns (sandbox_backend, instance_id) when inside a Runner 消息周转期。"""
    return _active.get()


def set_runner_sandbox(backend: Any, instance_id: str) -> Any:
    return _active.set((backend, instance_id))


def reset_runner_sandbox(token: Any) -> None:
    _active.reset(token)


def clear_runner_sandbox() -> None:
    _active.set(None)


__all__ = [
    "clear_runner_sandbox",
    "get_runner_sandbox",
    "reset_runner_sandbox",
    "set_runner_sandbox",
]
