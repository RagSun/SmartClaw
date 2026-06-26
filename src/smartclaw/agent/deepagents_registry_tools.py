"""
将 SmartClaw ToolRegistry 中的工具转为 LangChain StructuredTool，
供 create_deep_agent(..., tools=...) 与 DeepAgents 内置工具合并使用。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, create_model

# 与 DeepAgents 默认工具集重名时跳过，避免重复定义与路由歧义
RESERVED_DEEPAGENTS_TOOL_NAMES = frozenset(
    {
        "write_todos",
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "execute",
        "task",
    }
)


class _smartclawToolArgsBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _json_schema_to_python_type(spec: dict[str, Any]) -> Any:
    """粗粒度 JSON Schema -> typing，满足多数内置工具参数。"""
    if "enum" in spec:
        return str
    t = spec.get("type", "string")
    if t == "string":
        return str
    if t == "integer":
        return int
    if t == "number":
        return float
    if t == "boolean":
        return bool
    if t == "array":
        return list[Any]
    if t == "object":
        return dict[str, Any]
    return str


def _args_model_for_tool(tool_name: str, parameters: dict[str, Any]) -> type[BaseModel]:
    props = (parameters or {}).get("properties") or {}
    required = set((parameters or {}).get("required") or [])
    if not props:
        safe_e = "".join(c if c.isalnum() else "_" for c in tool_name) or "tool"
        return create_model(
            f"smartclawDA_empty_{safe_e}",
            __base__=_smartclawToolArgsBase,
        )

    fields: dict[str, tuple[Any, Any]] = {}
    for pname, pspec in props.items():
        if not isinstance(pspec, dict):
            pspec = {}
        py_t = _json_schema_to_python_type(pspec)
        desc = (pspec.get("description") or "").strip()
        if pname in required:
            fields[pname] = (
                py_t,
                Field(description=desc) if desc else Field(),
            )
        else:
            fields[pname] = (
                py_t | None,
                Field(default=None, description=desc) if desc else Field(default=None),
            )

    safe = "".join(c if c.isalnum() else "_" for c in tool_name) or "tool"
    return create_model(
        f"smartclawDA_{safe}",
        __base__=_smartclawToolArgsBase,
        **fields,
    )


def _make_bound_invoke(registry: Any, tool_name: str) -> tuple[Any, Any]:
    """返回 (sync_fn, async_fn)。LangGraph ToolNode 走同步 invoke，仅 coroutine 会触发 NotImplementedError。"""

    async def _invoke_async(**kwargs: Any) -> str:
        res = await registry.execute(tool_name, kwargs)
        if res.success:
            r = res.result
            if isinstance(r, str):
                return r
            try:
                return json.dumps(r, ensure_ascii=False)
            except TypeError:
                return str(r)
        return f"[工具失败] {res.error or 'unknown'}"

    def _invoke_sync(**kwargs: Any) -> str:
        ctx = contextvars.copy_context()

        def _run_in_fresh_loop() -> str:
            return ctx.run(lambda: asyncio.run(_invoke_async(**kwargs)))

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return _run_in_fresh_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_run_in_fresh_loop).result()

    return _invoke_sync, _invoke_async


def registry_tools_for_deepagents(registry: Any) -> list[Any]:
    """
    将 Registry 中工具（排除与 DeepAgents 内置重名者）转为 StructuredTool。
    """
    from langchain_core.tools import StructuredTool

    out: list[Any] = []
    for rt in registry.iter_registered():
        name = rt.definition.name
        if name in RESERVED_DEEPAGENTS_TOOL_NAMES:
            continue
        desc = (rt.definition.description or "").strip() or name
        schema = rt.definition.parameters or {"type": "object", "properties": {}}
        args_model = _args_model_for_tool(name, schema)
        sync_fn, async_fn = _make_bound_invoke(registry, name)
        out.append(
            StructuredTool.from_function(
                name=name,
                description=desc,
                func=sync_fn,
                coroutine=async_fn,
                args_schema=args_model,
            )
        )
    return out
