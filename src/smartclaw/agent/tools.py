"""
工具注册表模块（兼容旧接口）

兼容旧代码，从 tools.registry 导入。
"""

from smartclaw.agent.tools.registry import (
    ToolRegistry,
    get_tool_registry,
)

__all__ = [
    "ToolRegistry",
    "get_tool_registry",
]
