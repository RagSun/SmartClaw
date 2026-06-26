"""exec 执行期上下文：供 ExecTool 读取当前 Agent 的 agent.json 快照（避免每次查盘）。"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Optional

_agent_config_snapshot: ContextVar[Optional[dict[str, Any]]] = ContextVar(
    "smartclaw_agent_config_snapshot", default=None
)

# Runner 注入：与 Runner.start DeepAgents 工作区相同的磁盘 agent.json ∪ profile 合并快照
_workspace_resolution_snap: ContextVar[Optional[dict[str, Any]]] = ContextVar(
    "smartclaw_workspace_resolution_snap", default=None
)


def set_agent_config_for_exec(config: Optional[dict[str, Any]]) -> Any:
    """返回 token，供 reset 使用。"""
    return _agent_config_snapshot.set(config)


def reset_agent_config_for_exec(token: Any) -> None:
    _agent_config_snapshot.reset(token)


def get_agent_config_for_exec() -> Optional[dict[str, Any]]:
    return _agent_config_snapshot.get()


def set_workspace_resolution_snap(snap: Optional[dict[str, Any]]) -> Any:
    """与 set_agent_config_for_exec 配对，供 read/write 与 exec 门禁解析同一 workspace 根。"""
    return _workspace_resolution_snap.set(snap)


def reset_workspace_resolution_snap(token: Any) -> None:
    _workspace_resolution_snap.reset(token)


def get_workspace_resolution_snap() -> Optional[dict[str, Any]]:
    """未注入时为 None。"""
    return _workspace_resolution_snap.get()


__all__ = [
    "get_workspace_resolution_snap",
    "reset_agent_config_for_exec",
    "reset_workspace_resolution_snap",
    "set_agent_config_for_exec",
    "set_workspace_resolution_snap",
    "get_agent_config_for_exec",
]
