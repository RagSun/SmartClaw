"""
文件读取工具

允许 Agent 读取文件内容。
"""

from pathlib import Path
from typing import Any

from smartclaw.agent.workspace_bound_backend import normalize_workspace_tool_path


def _workspace_roots_detail() -> tuple[list[Path], str | None]:
    from smartclaw.agent.workspace import file_tool_workspace_roots_detail

    return file_tool_workspace_roots_detail()


def _resolve_requested_path(path: str, roots: list[Path]) -> Path:
    normalized = normalize_workspace_tool_path(path)
    p = Path(normalized)
    if p.is_absolute() and not normalized.startswith("/"):
        return p.resolve()
    workspace_root = roots[-1] if roots else Path.cwd()
    rel = normalized.lstrip("/\\")
    return (workspace_root / rel).resolve()


class ReadTool:
    """文件读取工具"""

    def __init__(self):
        self.name = "read_file"
        self.description = "读取文件内容"
        self.parameters = {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "limit": {
                    "type": "integer",
                    "description": "最多读取的行数，默认全部",
                    "default": 0,
                },
            },
            "required": ["path"],
        }

    async def execute(self, path: str, limit: int = 0) -> dict[str, Any]:
        """读取文件"""
        try:
            roots, unresolved_hint = _workspace_roots_detail()
            file_path = _resolve_requested_path(path, roots)

            if not file_path.exists():
                return {"success": False, "error": f"文件不存在: {path}", "content": ""}

            if not file_path.is_file():
                return {"success": False, "error": f"不是文件: {path}", "content": ""}

            # 安全限制：只允许读取临时目录、root 目录和当前租户 Agent workspace。
            allowed = any(file_path.is_relative_to(root) for root in roots)
            if not allowed:
                suf = f" {unresolved_hint}" if unresolved_hint else ""
                return {"success": False, "error": f"路径访问被拒绝。{suf}".strip(), "content": ""}

            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = (
                    f.read()
                    if limit <= 0
                    else "".join(f.readline() for _ in range(limit))
                )

            max_chars = 100000
            if len(content) > max_chars:
                content = content[:max_chars] + "\n... (内容过长，已截断)"

            return {
                "success": True,
                "content": content,
                "path": str(file_path),
                "size": file_path.stat().st_size,
            }

        except Exception as e:
            return {"success": False, "error": str(e), "content": ""}


async def read_handler(path: str, limit: int = 0) -> str:
    """read_file 工具的处理函数"""
    tool = ReadTool()
    result = await tool.execute(path, limit)
    if result["success"]:
        return result["content"]
    else:
        return f"错误: {result['error']}"
