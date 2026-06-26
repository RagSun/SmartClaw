"""兼容别名：请改用 ``smartclaw.tool_packages.loader``。"""

import warnings

from smartclaw.tool_packages.loader import ToolLoader

warnings.warn(
    "smartclaw.tool.loader 已移至 smartclaw.tool_packages.loader，请更新 import。",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["ToolLoader"]
