"""
外置工具包管理器

负责扩展工具包（带 tool.json）的安装、卸载、列表等；与 ToolRegistry 中已注册函数不是同一概念。
"""

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from smartclaw.console import error, info, success, warning
from smartclaw.subprocess_io import SUBPROCESS_TEXT_KWARGS


@dataclass
class ToolInfo:
    """已安装扩展包元数据。"""

    name: str
    version: str
    description: str
    path: Path
    functions: list[dict[str, Any]]
    enabled: bool = True


class ToolManager:
    """管理 ~/.smartclaw/tools 下的外置工具包。"""

    def __init__(self, tools_dir: Optional[Path] = None):
        self.tools_dir = tools_dir or Path.home() / ".smartclaw" / "tools"
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        self._tools: dict[str, ToolInfo] = {}
        self._load_tools()

    def _load_tools(self) -> None:
        for tool_dir in self.tools_dir.iterdir():
            if not tool_dir.is_dir():
                continue

            tool_json = tool_dir / "tool.json"
            if not tool_json.exists():
                continue

            try:
                with open(tool_json, encoding="utf-8") as f:
                    data = json.load(f)

                tool_info = ToolInfo(
                    name=data.get("name", tool_dir.name),
                    version=data.get("version", "0.0.0"),
                    description=data.get("description", ""),
                    path=tool_dir,
                    functions=data.get("functions", []),
                    enabled=data.get("enabled", True),
                )

                self._tools[tool_info.name] = tool_info

            except Exception as e:
                warning(f"加载工具失败: {tool_dir.name} - {e}")

    def install(self, source: str) -> bool:
        info(f"安装工具: {source}")

        if source.startswith(("http://", "https://", "git@")):
            return self._install_from_git(source)
        if Path(source).exists():
            return self._install_from_local(Path(source))
        return self._install_from_pypi(source)

    def _install_from_git(self, url: str) -> bool:
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                ["git", "clone", "--depth", "1", url, tmpdir],
                capture_output=True,
                text=True,
                **SUBPROCESS_TEXT_KWARGS,
            )

            if result.returncode != 0:
                error(f"Git 克隆失败: {result.stderr}")
                return False

            tool_json = Path(tmpdir) / "tool.json"
            if not tool_json.exists():
                error("工具包缺少 tool.json 文件")
                return False

            with open(tool_json, encoding="utf-8") as f:
                data = json.load(f)

            tool_name = data.get("name")
            if not tool_name:
                error("tool.json 缺少 name 字段")
                return False

            dest_dir = self.tools_dir / tool_name
            if dest_dir.exists():
                shutil.rmtree(dest_dir)

            shutil.copytree(tmpdir, dest_dir)

            self._load_tools()

            success(f"工具安装成功: {tool_name}")
            return True

    def _install_from_local(self, path: Path) -> bool:
        tool_json = path / "tool.json"

        if not tool_json.exists():
            error(f"工具包缺少 tool.json: {path}")
            return False

        with open(tool_json, encoding="utf-8") as f:
            data = json.load(f)

        tool_name = data.get("name")
        if not tool_name:
            error("tool.json 缺少 name 字段")
            return False

        dest_dir = self.tools_dir / tool_name
        if dest_dir.exists():
            shutil.rmtree(dest_dir)

        shutil.copytree(path, dest_dir)

        self._load_tools()

        success(f"工具安装成功: {tool_name}")
        return True

    def _install_from_pypi(self, package: str) -> bool:
        result = subprocess.run(
            ["pip", "install", package],
            capture_output=True,
            text=True,
            **SUBPROCESS_TEXT_KWARGS,
        )

        if result.returncode != 0:
            error(f"PyPI 安装失败: {result.stderr}")
            return False

        success(f"包安装成功: {package}")
        return True

    def uninstall(self, name: str) -> bool:
        if name not in self._tools:
            error(f"工具不存在: {name}")
            return False

        tool_dir = self._tools[name].path
        shutil.rmtree(tool_dir)
        del self._tools[name]

        success(f"工具已卸载: {name}")
        return True

    def list(self) -> list[ToolInfo]:
        return list(self._tools.values())

    def get(self, name: str) -> Optional[ToolInfo]:
        return self._tools.get(name)

    def enable(self, name: str) -> bool:
        if name not in self._tools:
            return False

        tool = self._tools[name]
        tool.enabled = True
        self._save_tool_config(tool)
        return True

    def disable(self, name: str) -> bool:
        if name not in self._tools:
            return False

        tool = self._tools[name]
        tool.enabled = False
        self._save_tool_config(tool)
        return True

    def _save_tool_config(self, tool: ToolInfo) -> None:
        tool_json = tool.path / "tool.json"

        with open(tool_json, encoding="utf-8") as f:
            data = json.load(f)

        data["enabled"] = tool.enabled

        with open(tool_json, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


_global_manager: Optional[ToolManager] = None


def get_tool_manager() -> ToolManager:
    global _global_manager

    if _global_manager is None:
        _global_manager = ToolManager()

    return _global_manager
