"""Load tenant workspace tools from manifest files into ToolRegistry."""

from __future__ import annotations

import importlib.util
import inspect
import json
import re
from pathlib import Path
from typing import Any

from smartclaw.agent.tools.metadata import metadata_for_tool
from smartclaw.agent.tools.registry import ToolRegistry, get_tool_registry
from smartclaw.console import info, warning


_REGISTERED_WORKSPACE_TOOLS: set[str] = set()
_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{1,63}$")
_SUSPICIOUS_PATTERNS = (
    "api_key=",
    "secret_key=",
    "access_key_secret",
    "rm -rf /",
    "powershell -encodedcommand",
)


def _read_json(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("tool.json 必须是 JSON object")
    return data


def _scan_source(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").lower()
    except Exception as exc:
        return [f"source read failed: {exc}"]
    findings = [item for item in _SUSPICIOUS_PATTERNS if item in text]
    if "tvly-" in text or "sk-" in text:
        findings.append("possible hard-coded secret literal")
    return sorted(set(findings))


def _load_handler(module_path: Path, function_name: str) -> Any:
    module_name = f"smartclaw_workspace_tool_{module_path.parent.name}_{module_path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if not spec or not spec.loader:
        raise ValueError(f"无法加载工具模块: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    handler = getattr(module, function_name, None)
    if not callable(handler):
        raise ValueError(f"工具函数不存在或不可调用: {function_name}")
    return handler


def _build_definition(manifest: dict[str, Any], fallback_name: str) -> tuple[str, str, dict[str, Any], int]:
    name = str(manifest.get("name") or fallback_name).strip()
    if not _TOOL_NAME_RE.match(name):
        raise ValueError(f"工具名称非法: {name!r}")
    description = str(manifest.get("description") or f"Workspace tool {name}").strip()
    parameters = manifest.get("parameters") or {"type": "object", "properties": {}}
    if not isinstance(parameters, dict):
        raise ValueError("parameters 必须是 JSON Schema object")
    timeout_ms = int(manifest.get("timeout_ms") or 30000)
    timeout_ms = max(1000, min(timeout_ms, 300000))
    return name, description, parameters, timeout_ms


def register_workspace_tools(
    workspace_root: str | Path,
    *,
    registry: ToolRegistry | None = None,
    fail_on_error: bool = False,
) -> dict[str, Any]:
    """Register tools from ``workspace/tools/*/tool.json``.

    Each tool package uses:
    - ``tool.json``: name, description, parameters, entry, entry_function, metadata.
    - ``handler.py`` or configured entry: Python module containing the handler.
    """
    root = Path(workspace_root).resolve()
    tools_root = root / "tools"
    reg = registry or get_tool_registry()
    loaded: list[str] = []
    skipped: list[dict[str, str]] = []
    if not tools_root.is_dir():
        return {"success": True, "loaded": loaded, "skipped": skipped, "workspace": str(root)}

    for manifest_path in sorted(tools_root.glob("*/tool.json")):
        tool_dir = manifest_path.parent.resolve()
        try:
            tool_dir.relative_to(tools_root.resolve())
            manifest = _read_json(manifest_path)
            if manifest.get("enabled") is False:
                skipped.append({"tool": tool_dir.name, "reason": "disabled"})
                continue
            name, description, parameters, timeout_ms = _build_definition(manifest, tool_dir.name)
            entry = str(manifest.get("entry") or "handler.py").strip()
            function_name = str(manifest.get("entry_function") or "handler").strip()
            module_path = (tool_dir / entry).resolve()
            module_path.relative_to(tool_dir)
            if not module_path.is_file():
                raise ValueError(f"entry 文件不存在: {entry}")
            findings = _scan_source(module_path)
            if findings and not manifest.get("allow_security_findings"):
                raise ValueError("安全扫描未通过: " + ", ".join(findings))
            if reg.get(name) and name not in _REGISTERED_WORKSPACE_TOOLS and not manifest.get("allow_override"):
                raise ValueError(f"拒绝覆盖内置或已存在工具: {name}")
            handler = _load_handler(module_path, function_name)
            meta = metadata_for_tool(
                name,
                **{
                    "owner": manifest.get("owner") or "workspace",
                    "version": str(manifest.get("version") or "0.1.0"),
                    "risk_level": manifest.get("risk_level") or "high",
                    "tenant_scope": "workspace",
                    "lifecycle": "workspace",
                    "test_status": manifest.get("test_status") or "unknown",
                },
            )
            reg.register(
                name=name,
                description=description,
                handler=handler,
                parameters=parameters,
                timeout_ms=timeout_ms,
                metadata=meta,
            )
            _REGISTERED_WORKSPACE_TOOLS.add(name)
            loaded.append(name)
            info(
                f"[WorkspaceTools] registered {name} from {module_path.name} "
                f"(async={inspect.iscoroutinefunction(handler)})"
            )
        except Exception as exc:
            skipped.append({"tool": tool_dir.name, "reason": str(exc)})
            warning(f"[WorkspaceTools] skip {tool_dir.name}: {exc}")
            if fail_on_error:
                raise
    return {"success": True, "loaded": loaded, "skipped": skipped, "workspace": str(root)}


__all__ = ["register_workspace_tools"]
