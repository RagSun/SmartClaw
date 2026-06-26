"""
工具注册表

管理 Agent 可用的工具，提供工具定义和执行接口。
这是一个独立的模块，不依赖任何工具实现。

门禁分层（便于与文档对照）：
- ``execute`` 在调用 handler 前，若存在 ``ToolSecurityContext``，依次走
  ``_run_registry_invoke_gates``：平面 B（agent.json denied/enforce）、
  平面 C（``check_tool_allowed``）、二次确认。
- 宿主命令字符串的评估见 ``host_command_gate.evaluate_host_command``（与工具名门禁正交）。
"""

import asyncio
import contextvars
import inspect
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from smartclaw.audit.logger import audit_tool
from smartclaw.auth.tool_gate import (
    ToolSecurityContext,
    check_tool_allowed,
    get_tool_security_context,
)
from smartclaw.config.loader import get_config
from smartclaw.console import info, warning
from smartclaw.interfaces import ToolDefinition, ToolResult
from smartclaw.logging_utils import safe_preview, summarize_payload


@dataclass
class RegisteredTool:
    """已注册的工具"""

    definition: ToolDefinition
    handler: Callable
    is_async: bool = False
    timeout_ms: int = 30000
    metadata: dict[str, Any] | None = None

    def metadata_snapshot(self) -> dict[str, Any]:
        """Return normalized, non-secret metadata for catalog and audit output."""
        base = {
            "owner": "",
            "version": "0.1.0",
            "risk_level": "medium",
            "test_status": "unknown",
            "tenant_scope": "tenant",
            "audit_required": True,
            "lifecycle": "runtime",
        }
        if self.metadata:
            base.update(self.metadata)
        return base


class ToolRegistry:
    """
    工具注册表

    管理工具的注册、查找、执行。
    """

    def __init__(self):
        """初始化工具注册表"""
        self._tools: dict[str, RegisteredTool] = {}
        # 沙箱上下文
        self.sandbox_backend = None
        self.sandbox_instance_id = None

    def set_sandbox_context(self, backend, instance_id):
        """遗留：将沙箱绑定到进程级注册表。主路径已改为 asyncio 任务 ContextVar（sandbox_context）。"""
        self.sandbox_backend = backend
        self.sandbox_instance_id = instance_id
        info(f"工具注册表已绑定沙箱: {instance_id}")

    def clear_sandbox_context_if_match(self, instance_id: str | None) -> None:
        """仅在 instance_id 与当前绑定一致时解绑，避免多 Runner stop 误清其它 Agent 的沙箱指针。"""
        if not instance_id or self.sandbox_instance_id != instance_id:
            return
        self.sandbox_backend = None
        self.sandbox_instance_id = None
        info(f"工具注册表已解除沙箱绑定（实例 {instance_id}）")

    def register(
        self,
        name: str,
        description: str,
        handler: Callable,
        parameters: Optional[dict[str, Any]] = None,
        timeout_ms: int = 30000,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """
        注册工具

        参数:
            name: 工具名称
            description: 工具描述
            handler: 处理函数
            parameters: 参数定义（JSON Schema）
            timeout_ms: 执行超时时间
            metadata: 原子级工具元数据，供审计、权限和能力目录使用
        """
        definition = ToolDefinition(
            name=name,
            description=description,
            parameters=parameters or {},
        )

        is_async = inspect.iscoroutinefunction(handler)

        self._tools[name] = RegisteredTool(
            definition=definition,
            handler=handler,
            is_async=is_async,
            timeout_ms=timeout_ms,
            metadata=metadata or {},
        )

        info(f"注册工具: {name} (async={is_async})")

    def unregister(self, name: str) -> None:
        """注销工具"""
        if name in self._tools:
            del self._tools[name]
            info(f"注销工具: {name}")

    def get(self, name: str) -> Optional[RegisteredTool]:
        """获取工具"""
        return self._tools.get(name)

    def list_all(self) -> list[ToolDefinition]:
        """列出所有工具定义"""
        return [t.definition for t in self._tools.values()]

    def iter_registered(self) -> list[RegisteredTool]:
        """按注册顺序返回全部工具（供 DeepAgents 等挂载扩展工具）。"""
        return list(self._tools.values())

    def describe(self, name: str) -> Optional[dict[str, Any]]:
        """Return a complete tool catalog entry for one registered tool."""
        tool = self.get(name)
        if not tool:
            return None
        return {
            "name": tool.definition.name,
            "description": tool.definition.description,
            "parameters": tool.definition.parameters,
            "is_async": tool.is_async,
            "timeout_ms": tool.timeout_ms,
            "metadata": tool.metadata_snapshot(),
        }

    def list_catalog(self) -> list[dict[str, Any]]:
        """Return all registered tools as operator-facing catalog rows."""
        rows: list[dict[str, Any]] = []
        for tool in self._tools.values():
            rows.append(
                {
                    "name": tool.definition.name,
                    "description": tool.definition.description,
                    "is_async": tool.is_async,
                    "timeout_ms": tool.timeout_ms,
                    "metadata": tool.metadata_snapshot(),
                }
            )
        return rows

    @staticmethod
    def _normalize_name_list(value: Any) -> set[str]:
        if not value:
            return set()
        if isinstance(value, str):
            return {item.strip() for item in value.split(",") if item.strip()}
        if isinstance(value, (list, tuple, set)):
            return {str(item).strip() for item in value if str(item).strip()}
        return set()

    @staticmethod
    def _agent_tool_policy(agent_cfg: dict[str, Any]) -> tuple[set[str], set[str], bool]:
        tool_cfg = agent_cfg.get("tools") if isinstance(agent_cfg.get("tools"), dict) else {}
        allowed = set()
        denied = set()
        allowed.update(ToolRegistry._normalize_name_list(agent_cfg.get("allowed_tools")))
        allowed.update(ToolRegistry._normalize_name_list(tool_cfg.get("allowed")))
        allowed.update(ToolRegistry._normalize_name_list(tool_cfg.get("allowed_tools")))
        denied.update(ToolRegistry._normalize_name_list(agent_cfg.get("denied_tools")))
        denied.update(ToolRegistry._normalize_name_list(tool_cfg.get("denied")))
        denied.update(ToolRegistry._normalize_name_list(tool_cfg.get("denied_tools")))
        enforce = bool(agent_cfg.get("enforce_allowed_tools", False) or tool_cfg.get("enforce_allowed_tools", False))
        return allowed, denied, enforce

    @staticmethod
    def _requires_confirmation(tool: RegisteredTool, parameters: dict[str, Any]) -> bool:
        meta = tool.metadata_snapshot()
        if parameters.get("confirm") is True or parameters.get("confirmed") is True:
            return False
        if meta.get("requires_confirmation") is True:
            return True
        return str(meta.get("risk_level", "")).lower() in {"critical"}

    @staticmethod
    def _confirmation_payload(name: str, tool: RegisteredTool, parameters: dict[str, Any]) -> dict[str, Any]:
        return {
            "success": False,
            "requires_confirmation": True,
            "tool": name,
            "risk_level": tool.metadata_snapshot().get("risk_level", "unknown"),
            "params_preview": summarize_payload(parameters),
            "message": f"工具 {name} 需要二次确认。确认后请再次调用并传入 confirm=true。",
        }

    @staticmethod
    def _run_registry_invoke_gates(
        tctx: ToolSecurityContext,
        name: str,
        tool: RegisteredTool,
        parameters: dict[str, Any],
    ) -> Optional[ToolResult]:
        """
        Registry 路径（P2/P3）在进入 handler 前的固定顺序门禁。

        1. 读 agent.json → 平面 B：``name in denied`` → ``enforce & name not in allowed``
        2. 平面 C：``check_tool_allowed(name, …)``
        3. 二次确认（metadata / critical 风险）

        返回 ``ToolResult`` 表示应短路返回；返回 ``None`` 表示继续执行 handler。
        异常时返回失败 ``ToolResult``（与原先 try/except 行为一致）。
        """
        try:
            cfg = get_config()
            agent_cfg: dict[str, Any] = {}
            try:
                from smartclaw.agent.manager import AgentManager

                agent_cfg = AgentManager()._read_config(
                    tctx.agent_id,
                    tenant_id=tctx.tenant_id,
                ) or {}
            except Exception as ex:
                warning(f"读取 Agent 工具策略失败（继续使用全局门禁）: {ex}")

            allowed, denied, enforce_allowed = ToolRegistry._agent_tool_policy(agent_cfg)
            meta = tool.metadata_snapshot()

            if name in denied:
                reason = f"Agent {tctx.agent_id} 禁止调用工具: {name}"
                audit_tool(
                    tenant_id=tctx.tenant_id,
                    user_open_id=tctx.feishu_open_id,
                    agent_id=tctx.agent_id,
                    tool_name=name,
                    success=False,
                    error=reason,
                    metadata=meta,
                )
                return ToolResult(tool_name=name, success=False, result=None, error=reason)

            if enforce_allowed and name not in allowed:
                reason = f"Agent {tctx.agent_id} 未在 allowed_tools 中声明工具: {name}"
                audit_tool(
                    tenant_id=tctx.tenant_id,
                    user_open_id=tctx.feishu_open_id,
                    agent_id=tctx.agent_id,
                    tool_name=name,
                    success=False,
                    error=reason,
                    metadata=meta,
                )
                return ToolResult(tool_name=name, success=False, result=None, error=reason)

            ok, reason = check_tool_allowed(name, tctx, cfg)
            if not ok:
                audit_tool(
                    tenant_id=tctx.tenant_id,
                    user_open_id=tctx.feishu_open_id,
                    agent_id=tctx.agent_id,
                    tool_name=name,
                    success=False,
                    error=reason,
                    metadata=meta,
                )
                return ToolResult(
                    tool_name=name,
                    success=False,
                    result=None,
                    error=reason,
                )

            if ToolRegistry._requires_confirmation(tool, parameters):
                payload = ToolRegistry._confirmation_payload(name, tool, parameters)
                audit_tool(
                    tenant_id=tctx.tenant_id,
                    user_open_id=tctx.feishu_open_id,
                    agent_id=tctx.agent_id,
                    tool_name=name,
                    success=False,
                    error="requires_confirmation",
                    metadata=meta,
                )
                return ToolResult(
                    tool_name=name,
                    success=False,
                    result=payload,
                    error=payload["message"],
                )

        except Exception as e:
            warning(f"工具门禁检查异常(已拒绝): {e}")
            return ToolResult(
                tool_name=name,
                success=False,
                result=None,
                error=f"工具门禁检查异常: {e}",
            )

        return None

    async def execute(
        self,
        name: str,
        parameters: dict[str, Any],
    ) -> ToolResult:
        """执行工具"""
        tool = self.get(name)

        if not tool:
            return ToolResult(
                tool_name=name,
                success=False,
                result=None,
                error=f"工具不存在: {name}",
            )

        from smartclaw.agent.tools.loop_detector import (
            format_tool_invocation_line,
            get_loop_detector,
        )

        inv_line = format_tool_invocation_line(name, parameters)
        det = get_loop_detector()
        if det:
            lr = det.check_proposed(name, inv_line)
            if lr.is_loop:
                warning(
                    f"[LoopDetect] 阻断工具 {name}: 已连续 {lr.repeated_count} 次相同调用"
                )
                tctx_e = get_tool_security_context()
                if tctx_e:
                    audit_tool(
                        tenant_id=tctx_e.tenant_id,
                        user_open_id=tctx_e.feishu_open_id,
                        agent_id=tctx_e.agent_id,
                        tool_name=name,
                        success=False,
                        error="loop_detect:blocked",
                        metadata=tool.metadata_snapshot(),
                    )
                return ToolResult(
                    tool_name=name,
                    success=False,
                    result=None,
                    error=lr.suggested_action,
                )

        tctx = get_tool_security_context()
        if tctx:
            blocked = self._run_registry_invoke_gates(tctx, name, tool, parameters)
            if blocked is not None:
                return blocked

        t0 = time.perf_counter()
        tenant = getattr(tctx, "tenant_id", "default") if tctx else "default"
        agent_id = getattr(tctx, "agent_id", "") if tctx else ""
        session_id = getattr(tctx, "session_id", "") if tctx else ""
        info(
            "[ToolRegistry] tool start | "
            f"tenant={tenant} agent={agent_id} session={session_id} "
            f"tool={name} timeout_ms={tool.timeout_ms} params={summarize_payload(parameters)}"
        )
        try:
            if tool.is_async:
                result = await asyncio.wait_for(
                    tool.handler(**parameters),
                    timeout=tool.timeout_ms / 1000,
                )
            else:
                ctx = contextvars.copy_context()
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: ctx.run(tool.handler, **parameters),
                )

            result_wrapped = ToolResult(
                tool_name=name,
                success=True,
                result=result,
            )

        except asyncio.TimeoutError:
            result_wrapped = ToolResult(
                tool_name=name,
                success=False,
                result=None,
                error=f"工具执行超时: {name}",
            )
        except Exception as e:
            result_wrapped = ToolResult(
                tool_name=name,
                success=False,
                result=None,
                error=f"工具执行错误: {e}",
            )
        elapsed = time.perf_counter() - t0
        if result_wrapped.success:
            info(
                "[ToolRegistry] tool end | "
                f"tenant={tenant} agent={agent_id} session={session_id} "
                f"tool={name} status=ok elapsed={elapsed:.2f}s "
                f"result_preview={safe_preview(result_wrapped.result, 120)!r}"
            )
        else:
            warning(
                "[ToolRegistry] tool end | "
                f"tenant={tenant} agent={agent_id} session={session_id} "
                f"tool={name} status=error elapsed={elapsed:.2f}s "
                f"error={safe_preview(result_wrapped.error or '', 160)!r}"
            )
        if tctx:
            audit_tool(
                tenant_id=tctx.tenant_id,
                user_open_id=tctx.feishu_open_id,
                agent_id=tctx.agent_id,
                tool_name=name,
                success=result_wrapped.success,
                error=result_wrapped.error or "",
                metadata=tool.metadata_snapshot(),
            )
        if det:
            summary = (result_wrapped.error or "")[:500]
            if result_wrapped.success and not summary:
                try:
                    raw_res = result_wrapped.result
                    summary = (
                        json.dumps(raw_res, ensure_ascii=False, default=str)[:500]
                        if raw_res is not None
                        else ""
                    )
                except Exception:
                    summary = str(result_wrapped.result)[:500]
            det.record(
                inv_line,
                name,
                result_wrapped.success,
                summary,
            )
        return result_wrapped

    def get_openai_tools(self) -> list[dict[str, Any]]:
        """获取 OpenAI 格式的工具定义"""
        tools = []
        for tool in self._tools.values():
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.definition.name,
                        "description": tool.definition.description,
                        "parameters": tool.definition.parameters,
                    },
                }
            )
        return tools


# 全局工具注册表
_global_registry: Optional[ToolRegistry] = None


def get_tool_registry() -> ToolRegistry:
    """获取全局工具注册表"""
    global _global_registry

    if _global_registry is None:
        _global_registry = ToolRegistry()

    return _global_registry
