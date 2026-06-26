"""
从已安装的外置工具包加载入口模块，并将函数注册到 ToolRegistry。
"""

import importlib.util
import sys
from typing import Optional

from smartclaw.agent.tools import ToolRegistry, get_tool_registry
from smartclaw.console import info, warning
from smartclaw.tool_packages.manager import ToolInfo, ToolManager, get_tool_manager


class ToolLoader:
    """将 ~/.smartclaw/tools 下已启用包加载进全局 ToolRegistry。"""

    def __init__(
        self,
        tool_manager: Optional[ToolManager] = None,
        registry: Optional[ToolRegistry] = None,
    ):
        self.tool_manager = tool_manager or get_tool_manager()
        self.registry = registry or get_tool_registry()

    def load_all(self) -> int:
        loaded_count = 0

        for tool_info in self.tool_manager.list():
            if not tool_info.enabled:
                continue

            try:
                self.load_tool(tool_info)
                loaded_count += 1
            except Exception as e:
                warning(f"加载工具失败: {tool_info.name} - {e}")

        info(f"已加载 {loaded_count} 个工具")
        return loaded_count

    def load_tool(self, tool_info: ToolInfo) -> None:
        import json

        entry_file = tool_info.path / "tool.json"

        with open(entry_file, encoding="utf-8") as f:
            data = json.load(f)

        entry_module = data.get("entry", "main.py")
        module_path = tool_info.path / entry_module

        if not module_path.exists():
            raise FileNotFoundError(f"入口文件不存在: {module_path}")

        module_name = f"smartclaw_tool_{tool_info.name}"
        spec = importlib.util.spec_from_file_location(module_name, module_path)

        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载模块: {module_path}")

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        functions = data.get("functions", [])

        for func_def in functions:
            func_name = func_def.get("name")

            if not func_name:
                warning("函数定义缺少 name 字段")
                continue

            handler = getattr(module, func_name, None)

            if handler is None:
                warning(f"函数不存在: {func_name}")
                continue

            if not callable(handler):
                warning(f"不是可调用对象: {func_name}")
                continue

            self.registry.register_function(
                name=func_name,
                description=func_def.get("description", ""),
                handler=handler,
                parameters=func_def.get("parameters"),
                timeout_ms=func_def.get("timeout_ms", 30000),
            )

        info(f"加载工具: {tool_info.name} ({len(functions)} 个函数)")
