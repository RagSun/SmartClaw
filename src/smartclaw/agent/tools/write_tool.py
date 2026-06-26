"""
文件写入工具

允许 Agent 创建或覆盖文件。
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

    # DeepAgents/OpenClaw 风格：/docs/x、/workspace/docs/x、docs/x 均锚定当前 Agent workspace。
    workspace_root = roots[-1] if roots else Path.cwd()
    rel = normalized.lstrip("/\\")
    return (workspace_root / rel).resolve()


class WriteTool:
    """文件写入工具"""

    def __init__(self):
        self.name = "write_file"
        self.description = "写入内容到文件（会覆盖原有内容）"
        self.parameters = {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "content": {"type": "string", "description": "要写入的内容"},
            },
            "required": ["path", "content"],
        }

    async def execute(self, path: str, content: str) -> dict[str, Any]:
        """写入文件"""
        try:
            roots, unresolved_hint = _workspace_roots_detail()
            file_path = _resolve_requested_path(path, roots)
            is_allowed = any(file_path.is_relative_to(root) for root in roots)
            if not is_allowed:
                suf = f" {unresolved_hint}" if unresolved_hint else ""
                return {
                    "success": False,
                    "error": (
                        f"路径访问被拒绝，只允许写入 /tmp、/root 解析路径或当前 Agent 工作区。{suf}"
                    ).strip(),
                    "path": str(file_path),
                }

            file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)

            return {"success": True, "path": str(file_path), "size": len(content)}

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "path": str(file_path) if "file_path" in dir() else path,
            }


async def write_handler(path: str, content: str) -> str:
    """write_file 工具的处理函数"""
    tool = WriteTool()
    result = await tool.execute(path, content)
    if result["success"]:
        return f"文件已写入: {result['path']} ({result['size']} bytes)"
    else:
        return f"错误: {result['error']}"
