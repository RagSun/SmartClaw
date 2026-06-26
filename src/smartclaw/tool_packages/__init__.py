"""
外置工具包管理（CLI 安装/卸载）

与 ``agent/tools``（运行时可调用能力实现）区分：本包只做 ~/.smartclaw/tools 下扩展包的生命周期。
"""

from smartclaw.tool_packages.loader import ToolLoader
from smartclaw.tool_packages.manager import ToolManager, get_tool_manager

__all__ = ["ToolManager", "ToolLoader", "get_tool_manager"]
