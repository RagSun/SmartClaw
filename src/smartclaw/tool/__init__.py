"""兼容别名：请改用 ``smartclaw.tool_packages``。"""

import warnings

from smartclaw.tool_packages import ToolLoader, ToolManager, get_tool_manager

warnings.warn(
    "smartclaw.tool 已更名为 smartclaw.tool_packages（外置扩展包管理），请更新 import。",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["ToolManager", "ToolLoader", "get_tool_manager"]
