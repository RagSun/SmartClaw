"""
Agent 运行时模块

提供 Agent 生命周期管理、会话管理、策略配置等功能。
重导出采用惰性加载，避免 import agent.policy 时拉取 Runner（及可选 deepagents 依赖）。
"""

from typing import Any

from smartclaw.agent.policy import PolicyManager

__all__ = [
    "AgentRunner",
    "SessionManager",
    "ToolRegistry",
    "PolicyManager",
]


def __getattr__(name: str) -> Any:
    if name == "AgentRunner":
        from smartclaw.agent.runner import AgentRunner

        return AgentRunner
    if name == "SessionManager":
        from smartclaw.agent.session import SessionManager

        return SessionManager
    if name == "ToolRegistry":
        from smartclaw.agent.tools import ToolRegistry

        return ToolRegistry
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
