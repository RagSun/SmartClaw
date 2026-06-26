"""兼容别名：请改用 ``smartclaw.mcp.mcp_registry_bridge``。"""

import warnings

from smartclaw.mcp.mcp_registry_bridge import (
    McpToolSpec,
    register_mcp_tools_for_agent,
)

warnings.warn(
    "smartclaw.mcp.tool_provider 已更名为 mcp_registry_bridge（MCP→Registry 桥接），请更新 import。",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["McpToolSpec", "register_mcp_tools_for_agent"]
