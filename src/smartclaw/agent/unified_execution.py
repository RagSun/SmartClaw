"""
UnifiedExecutionEngine — AgentRunner 的统一编排入口（DeepAgents → ReAct → LLM+工具）。
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import uuid
from typing import TYPE_CHECKING, Any, Optional

from smartclaw.agent.history_deepagents import clip_history_for_deepagents
from smartclaw.agent.runner_exec_context import runner_exec_context
from smartclaw.agent.planner import ExecutionPlan, Planner
from smartclaw.agent.react import ExecutionResult
from smartclaw.config.loader import get_config
from smartclaw.console import debug, error, info, warning
from smartclaw.logging_utils import safe_preview
from smartclaw.monitoring.execution_trace import record_execution_event
from smartclaw.monitoring.metrics import record_execution_path_event
from smartclaw.core.event_bus import EventType
from smartclaw.memory.context_helpers import compact_prefix_from_memory_context

if TYPE_CHECKING:
    from smartclaw.agent.runner import AgentRunner
    from smartclaw.agent.session import Session


async def _emit_execution_bus(
    runner: Any,
    event_type: EventType,
    trace_id: str,
    session_id: str,
    tenant_id: str,
    data: Optional[dict[str, Any]] = None,
) -> None:
    bus = getattr(runner, "_event_bus", None)
    if not bus:
        return
    try:
        from smartclaw.core.event_bus import Event, EventLevel

        payload = dict(data or {})
        payload.setdefault("tenant_id", tenant_id)
        await bus.emit(
            Event(
                type=event_type,
                level=EventLevel.INFO,
                data=payload,
                agent_id=runner.agent_id,
                session_key=session_id,
                run_id=trace_id,
            )
        )
    except Exception as ex:
        error(f"EventBus emit failed: {ex}")


def _new_outcome_state() -> dict[str, Any]:
    return {
        "deepagents_attempted": False,
        "deepagents_outcome": "not_attempted",
        "react_attempted": False,
        "react_outcome": "not_attempted",
        "error_summary": "",
    }


def _truncate_err(msg: str, limit: int = 500) -> str:
    msg = msg or ""
    return msg if len(msg) <= limit else msg[:limit]


async def _emit_turn_outcome(
    runner: Any,
    *,
    trace_id: str,
    session_id: str,
    tenant_id: str,
    agent_id: str,
    emit: bool,
    primary_engine: str,
    outcome_state: dict[str, Any],
    depth: int,
) -> None:
    """本轮对用户可见结果的统一收口事件（方案 B），便于监控与排障。"""
    data = {
        "primary_engine": primary_engine,
        "depth": depth,
        "deepagents_attempted": outcome_state["deepagents_attempted"],
        "deepagents_outcome": outcome_state["deepagents_outcome"],
        "react_attempted": outcome_state["react_attempted"],
        "react_outcome": outcome_state["react_outcome"],
        "error_summary": _truncate_err(str(outcome_state.get("error_summary", ""))),
    }
    record_execution_event(
        event_type="turn_outcome",
        trace_id=trace_id,
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        data=data,
        emit=emit,
    )
    info(
        f"[turn_outcome] trace_id={trace_id} agent={agent_id} primary_engine={primary_engine} "
        f"deepagents={data['deepagents_outcome']} react={data['react_outcome']} depth={depth}"
    )
    await _emit_execution_bus(
        runner,
        EventType.EXECUTION_TURN_OUTCOME,
        trace_id,
        session_id,
        tenant_id,
        data,
    )
    record_execution_path_event("turn_outcome", trace_id=trace_id)


class UnifiedExecutionEngine:
    def __init__(self, runner: AgentRunner):
        self._runner = runner

    async def execute_turn(self, session: Session, depth: int = 0, max_depth: int = 10) -> str:
        cfg = get_config()
        ex = cfg.execution
        trace_id = str(uuid.uuid4())
        tenant_id = getattr(session, "tenant_id", "default")
        emit = ex.emit_structured_trace
        outcome_state = _new_outcome_state()
        if not ex.deepagents_enabled:
            outcome_state["deepagents_outcome"] = "disabled"
        if not ex.react_fallback_enabled:
            outcome_state["react_outcome"] = "disabled"

        record_execution_event(
            event_type="turn_start",
            trace_id=trace_id,
            agent_id=self._runner.agent_id,
            session_id=session.session_id,
            tenant_id=tenant_id,
            data={"depth": depth, "flags": ex.model_dump()},
            emit=emit,
        )
        await _emit_execution_bus(
            self._runner,
            EventType.EXECUTION_TURN_START,
            trace_id,
            session.session_id,
            tenant_id,
            {"depth": depth},
        )

        if depth >= max_depth:
            warning(f"Agent 执行达到最大递归深度 ({max_depth})，停止执行")
            record_execution_event(
                event_type="max_depth",
                trace_id=trace_id,
                agent_id=self._runner.agent_id,
                session_id=session.session_id,
                tenant_id=tenant_id,
                emit=emit,
            )
            await _emit_turn_outcome(
                self._runner,
                trace_id=trace_id,
                session_id=session.session_id,
                tenant_id=tenant_id,
                agent_id=self._runner.agent_id,
                emit=emit,
                primary_engine="max_depth",
                outcome_state=outcome_state,
                depth=depth,
            )
            return "工具执行已达最大次数，停止执行。"

        messages = await self._runner.session_manager.get_messages(session.session_id)
        if not session.context.get("load_history", True):
            if messages:
                messages = [messages[-1]]

        history = self._runner._build_message_history(messages)
        if self._runner._skills_prompt:
            history = [{"role": "system", "content": self._runner._skills_prompt}] + history

        user_message = ""
        for msg in reversed(history):
            if msg.get("role") == "user":
                user_message = msg.get("content", "") or ""
                break

        if self._runner._memory_manager and self._runner._context_mode != "minimal":
            self._runner._memory_manager.session_id = session.session_id
            self._runner._memory_manager.user_id = session.user_id
            if hasattr(self._runner._memory_manager, "tenant_id"):
                self._runner._memory_manager.tenant_id = tenant_id

            um_prev = (user_message or "").replace("\n", " ").strip()[:100]
            debug(
                "[Memory] inject start (unified) | "
                f"context_mode={self._runner._context_mode} "
                f"session_id={session.session_id!r} "
                f"fts_ready={self._runner._memory_manager.fts_ready} "
                f"retrieval_preview={um_prev!r}"
            )
            memory_context = self._runner._memory_manager.get_context_for_llm(
                include_stored_transcript=False,
                retrieval_query=user_message.strip() or None,
            )
            has_retrieval = bool(
                memory_context
                and any(
                    (m.get("content") or "").startswith("[记忆检索]")
                    for m in memory_context
                )
            )
            debug(
                "[Memory] inject done (unified) | "
                f"blocks={len(memory_context) if memory_context else 0} "
                f"has_retrieval={has_retrieval}"
            )
            if memory_context:
                if self._runner._context_mode == "full":
                    history = memory_context + history
                elif self._runner._context_mode == "compact":
                    prefix = compact_prefix_from_memory_context(memory_context)
                    if prefix:
                        history = prefix + history

        execution_plan_dict: Optional[dict[str, Any]] = None
        planner_obj: Optional[ExecutionPlan] = None

        if ex.planner_first_enabled and self._runner._llm_adapter:
            try:
                record_execution_event(
                    event_type="planner_start",
                    trace_id=trace_id,
                    agent_id=self._runner.agent_id,
                    session_id=session.session_id,
                    tenant_id=tenant_id,
                    data={"user_message_preview": user_message[:200]},
                    emit=emit,
                )
                await _emit_execution_bus(
                    self._runner,
                    EventType.EXECUTION_PLANNER_START,
                    trace_id,
                    session.session_id,
                    tenant_id,
                    {},
                )
                planner_input = user_message
                if self._runner._skills_prompt:
                    planner_input = f"{self._runner._skills_prompt}\n\nUser Request:\n{user_message}"

                planner = Planner(self._runner.tool_registry, self._runner._llm_adapter.chat)
                tools = self._runner.tool_registry.get_openai_tools()
                planner_obj = await planner.plan(planner_input, tools)
                execution_plan_dict = planner_obj.to_dict()
                skill_meta = getattr(self._runner, "_skill_runtime", None)
                if skill_meta and skill_meta.eligible_skills:
                    execution_plan_dict["candidate_skills"] = [
                        s.get("skill_key") or s.get("name")
                        for s in skill_meta.eligible_skills[:60]
                        if s.get("skill_key") or s.get("name")
                    ]
                self._runner._last_execution_plan = execution_plan_dict
                record_execution_event(
                    event_type="planner_done",
                    trace_id=trace_id,
                    agent_id=self._runner.agent_id,
                    session_id=session.session_id,
                    tenant_id=tenant_id,
                    data={
                        "step_count": len(planner_obj.steps),
                        "requires_subagent": planner_obj.requires_subagent,
                        "has_environment_plan": bool(
                            (planner_obj.environment_plan or "").strip()
                        ),
                    },
                    emit=emit,
                )
                await _emit_execution_bus(
                    self._runner,
                    EventType.EXECUTION_PLANNER_DONE,
                    trace_id,
                    session.session_id,
                    tenant_id,
                    {
                        "step_count": len(planner_obj.steps),
                        "requires_subagent": planner_obj.requires_subagent,
                        "has_environment_plan": bool(
                            (planner_obj.environment_plan or "").strip()
                        ),
                    },
                )
                record_execution_path_event("planner_ok", trace_id=trace_id)
            except Exception as e:
                error(f"[UnifiedEngine] Planner 前置失败，继续执行: {e}")
                record_execution_event(
                    event_type="planner_error",
                    trace_id=trace_id,
                    agent_id=self._runner.agent_id,
                    session_id=session.session_id,
                    tenant_id=tenant_id,
                    data={"error": str(e)},
                    emit=emit,
                )
                await _emit_execution_bus(
                    self._runner,
                    EventType.EXECUTION_PLANNER_ERROR,
                    trace_id,
                    session.session_id,
                    tenant_id,
                    {"error": str(e)},
                )
                record_execution_path_event("planner_error", trace_id=trace_id)

        if ex.deepagents_enabled and self._runner._deep_agent and depth == 0:
            try:
                outcome_state["deepagents_attempted"] = True
                history_for_deep = clip_history_for_deepagents(
                    history,
                    self._runner._skills_prompt,
                )
                if len(history_for_deep) < len(history):
                    info(f"[Runner] 历史消息已压缩: {len(history)} -> {len(history_for_deep)} 条")
                info(f"[Runner] 使用 DeepAgents 执行，记忆历史 {len(history_for_deep)} 条")
                record_execution_event(
                    event_type="deepagents_start",
                    trace_id=trace_id,
                    agent_id=self._runner.agent_id,
                    session_id=session.session_id,
                    tenant_id=tenant_id,
                    emit=emit,
                )
                await _emit_execution_bus(
                    self._runner,
                    EventType.EXECUTION_DEEPAGENTS_START,
                    trace_id,
                    session.session_id,
                    tenant_id,
                    {},
                )
                loop = asyncio.get_event_loop()
                sid = session.session_id
                from smartclaw.agent.tools.exec_context import (
                    reset_agent_config_for_exec,
                    reset_workspace_resolution_snap,
                    set_agent_config_for_exec,
                    set_workspace_resolution_snap,
                )

                ws_snap_ue = getattr(self._runner, "_workspace_resolution_snap", None)
                ws_tok_ue = set_workspace_resolution_snap(
                    ws_snap_ue if isinstance(ws_snap_ue, dict) else None
                )
                exec_cfg_tok = set_agent_config_for_exec(self._runner._full_agent_config or None)
                try:
                    ctx = contextvars.copy_context()
                    result = await loop.run_in_executor(
                        None,
                        lambda h=history_for_deep, p=execution_plan_dict, t=sid, c=ctx: c.run(
                            lambda: self._runner._deep_agent.run(
                                h,
                                execution_plan=p,
                                thread_id=t,
                            )
                        ),
                    )
                finally:
                    reset_agent_config_for_exec(exec_cfg_tok)
                    reset_workspace_resolution_snap(ws_tok_ue)

                info(
                    "[Runner] DeepAgents completed | "
                    f"trace={trace_id} tenant={tenant_id} agent={self._runner.agent_id} "
                    f"session={session.session_id} result_preview={safe_preview(result, 120)!r}"
                )
                record_execution_event(
                    event_type="deepagents_done",
                    trace_id=trace_id,
                    agent_id=self._runner.agent_id,
                    session_id=session.session_id,
                    tenant_id=tenant_id,
                    emit=emit,
                )
                await _emit_execution_bus(
                    self._runner,
                    EventType.EXECUTION_DEEPAGENTS_DONE,
                    trace_id,
                    session.session_id,
                    tenant_id,
                    {},
                )
                record_execution_path_event("deepagents_ok", trace_id=trace_id)
                outcome_state["deepagents_outcome"] = "ok"
                await _emit_turn_outcome(
                    self._runner,
                    trace_id=trace_id,
                    session_id=session.session_id,
                    tenant_id=tenant_id,
                    agent_id=self._runner.agent_id,
                    emit=emit,
                    primary_engine="deepagents",
                    outcome_state=outcome_state,
                    depth=depth,
                )
                return result
            except Exception as e:
                error(f"[Runner] DeepAgents 执行失败: {e}，使用原有引擎")
                record_execution_event(
                    event_type="deepagents_error",
                    trace_id=trace_id,
                    agent_id=self._runner.agent_id,
                    session_id=session.session_id,
                    tenant_id=tenant_id,
                    data={"error": str(e)},
                    emit=emit,
                )
                await _emit_execution_bus(
                    self._runner,
                    EventType.EXECUTION_DEEPAGENTS_ERROR,
                    trace_id,
                    session.session_id,
                    tenant_id,
                    {"error": str(e)},
                )
                record_execution_path_event("deepagents_fallback", trace_id=trace_id)
                outcome_state["deepagents_outcome"] = "error"
                outcome_state["error_summary"] = str(e)
        elif ex.deepagents_enabled and depth == 0:
            outcome_state["deepagents_outcome"] = "skipped_no_agent"
            record_execution_event(
                event_type="deepagents_skip",
                trace_id=trace_id,
                agent_id=self._runner.agent_id,
                session_id=session.session_id,
                tenant_id=tenant_id,
                data={"reason": "no_deep_agent"},
                emit=emit,
            )
            await _emit_execution_bus(
                self._runner,
                EventType.EXECUTION_DEEPAGENTS_SKIP,
                trace_id,
                session.session_id,
                tenant_id,
                {"reason": "no_deep_agent"},
            )

        if ex.react_fallback_enabled and self._runner._react_engine and self._runner._llm_adapter:
            try:
                outcome_state["react_attempted"] = True
                record_execution_event(
                    event_type="react_start",
                    trace_id=trace_id,
                    agent_id=self._runner.agent_id,
                    session_id=session.session_id,
                    tenant_id=tenant_id,
                    emit=emit,
                )
                await _emit_execution_bus(
                    self._runner,
                    EventType.EXECUTION_REACT_START,
                    trace_id,
                    session.session_id,
                    tenant_id,
                    {},
                )
                async with runner_exec_context(self._runner):
                    result: ExecutionResult = await self._runner._react_engine.execute(
                        user_message=user_message,
                        context=history,
                        system_prompt=self._runner._skills_prompt,
                        session_id=session.session_id,
                        prebuilt_plan=planner_obj,
                    )
                for step in result.steps:
                    if step.tool_name:
                        await self._runner.session_manager.add_message(
                            session_id=session.session_id,
                            role="assistant",
                            content=step.content,
                            tool_name=step.tool_name,
                        )
                record_execution_event(
                    event_type="react_done",
                    trace_id=trace_id,
                    agent_id=self._runner.agent_id,
                    session_id=session.session_id,
                    tenant_id=tenant_id,
                    emit=emit,
                )
                await _emit_execution_bus(
                    self._runner,
                    EventType.EXECUTION_REACT_DONE,
                    trace_id,
                    session.session_id,
                    tenant_id,
                    {},
                )
                record_execution_path_event("react_ok", trace_id=trace_id)
                outcome_state["react_outcome"] = "ok"
                await _emit_turn_outcome(
                    self._runner,
                    trace_id=trace_id,
                    session_id=session.session_id,
                    tenant_id=tenant_id,
                    agent_id=self._runner.agent_id,
                    emit=emit,
                    primary_engine="react",
                    outcome_state=outcome_state,
                    depth=depth,
                )
                return result.final_response
            except Exception as e:
                import traceback

                error(f"ReAct 执行失败: {e}\n完整 traceback:\n{traceback.format_exc()}")
                record_execution_event(
                    event_type="react_error",
                    trace_id=trace_id,
                    agent_id=self._runner.agent_id,
                    session_id=session.session_id,
                    tenant_id=tenant_id,
                    data={"error": str(e)},
                    emit=emit,
                )
                await _emit_execution_bus(
                    self._runner,
                    EventType.EXECUTION_REACT_ERROR,
                    trace_id,
                    session.session_id,
                    tenant_id,
                    {"error": str(e)},
                )
                record_execution_path_event("react_fallback", trace_id=trace_id)
                outcome_state["react_outcome"] = "error"
                outcome_state["error_summary"] = str(e)

        if (
            ex.react_fallback_enabled
            and depth == 0
            and not (self._runner._react_engine and self._runner._llm_adapter)
        ):
            outcome_state["react_outcome"] = "skipped_no_engine"

        if not ex.llm_tool_fallback_enabled:
            record_execution_event(
                event_type="llm_tool_skip",
                trace_id=trace_id,
                agent_id=self._runner.agent_id,
                session_id=session.session_id,
                tenant_id=tenant_id,
                emit=emit,
            )
            await _emit_turn_outcome(
                self._runner,
                trace_id=trace_id,
                session_id=session.session_id,
                tenant_id=tenant_id,
                agent_id=self._runner.agent_id,
                emit=emit,
                primary_engine="llm_tool_disabled",
                outcome_state=outcome_state,
                depth=depth,
            )
            return "执行路径已禁用 LLM+工具降级。"

        response = await self._runner._call_llm(history)
        tool_calls = response.get("tool_calls", []) or []

        if tool_calls:
            seen_calls: set[str] = set()
            unique_tool_calls = []
            for tc in tool_calls:
                tc_name = tc.get("name") or tc.get("function", {}).get("name", "")
                tc_params = json.dumps(tc.get("parameters") or tc.get("function", {}).get("arguments", {}))
                tc_key = f"{tc_name}:{tc_params}"
                if tc_key not in seen_calls:
                    seen_calls.add(tc_key)
                    unique_tool_calls.append(tc)

            if len(tool_calls) != len(unique_tool_calls):
                info(f"去重工具调用: {len(tool_calls)} -> {len(unique_tool_calls)}")

            for tool_call in unique_tool_calls:
                tool_name = tool_call.get("name") or tool_call.get("function", {}).get("name", "")
                tool_params = tool_call.get("parameters") or tool_call.get("function", {}).get("arguments", {})

                await self._runner.session_manager.add_message(
                    session_id=session.session_id,
                    role="assistant",
                    content="",
                    tool_name=tool_name,
                    tool_call_id=tool_call.get("id", ""),
                )

                tool_result = await self._runner._execute_tool(tool_name=tool_name, parameters=tool_params)

                await self._runner.session_manager.add_message(
                    session_id=session.session_id,
                    role="tool",
                    content=json.dumps(
                        tool_result.result if tool_result.success else tool_result.error
                    ),
                    tool_name=tool_name,
                    tool_call_id=tool_call.get("id", ""),
                )

            record_execution_event(
                event_type="llm_tool_recurse",
                trace_id=trace_id,
                agent_id=self._runner.agent_id,
                session_id=session.session_id,
                tenant_id=tenant_id,
                data={"depth": depth + 1},
                emit=emit,
            )
            await _emit_turn_outcome(
                self._runner,
                trace_id=trace_id,
                session_id=session.session_id,
                tenant_id=tenant_id,
                agent_id=self._runner.agent_id,
                emit=emit,
                primary_engine="llm_tool_recurse",
                outcome_state=outcome_state,
                depth=depth,
            )
            return await self.execute_turn(session, depth=depth + 1, max_depth=max_depth)

        if hasattr(response, "content"):
            out = response.content
        elif isinstance(response, dict):
            out = response.get("content", "")
        else:
            out = str(response)

        record_execution_event(
            event_type="turn_end",
            trace_id=trace_id,
            agent_id=self._runner.agent_id,
            session_id=session.session_id,
            tenant_id=tenant_id,
            emit=emit,
        )
        await _emit_execution_bus(
            self._runner,
            EventType.EXECUTION_TURN_END,
            trace_id,
            session.session_id,
            tenant_id,
            {},
        )
        await _emit_turn_outcome(
            self._runner,
            trace_id=trace_id,
            session_id=session.session_id,
            tenant_id=tenant_id,
            agent_id=self._runner.agent_id,
            emit=emit,
            primary_engine="llm_direct",
            outcome_state=outcome_state,
            depth=depth,
        )
        return out
