"""兼容别名：请改用 ``smartclaw.tool_packages.manager``。"""

import warnings

from smartclaw.tool_packages.manager import ToolInfo, ToolManager, get_tool_manager

warnings.warn(
    "smartclaw.tool.manager 已移至 smartclaw.tool_packages.manager，请更新 import。",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["ToolInfo", "ToolManager", "get_tool_manager"]
