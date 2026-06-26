"""
Agent 运行器模块

管理 Agent 的生命周期和执行循环。
"""

# DeepAgents 历史消息限制见 smartclaw.agent.history_deepagents

# 配置日志文件
import os
from pathlib import Path
_log_dir = Path.home() / ".smartclaw" / "logs"
_log_dir.mkdir(parents=True, exist_ok=True)
_log_file = _log_dir / "agent.log"
from smartclaw.console import configure_logging
configure_logging(str(_log_file), enabled=True)

import asyncio
import json
import time
from typing import Any, Optional

from smartclaw.agent.react import ExecutionResult, ReActEngine
from smartclaw.agent.deepagents_wrapper import DeepAgentsWrapper
from smartclaw.agent.control_flow import preflight_capabilities
from smartclaw.agent.history_deepagents import clip_history_for_deepagents
from smartclaw.agent.planner_executor import Planner, SimpleExecutor
from smartclaw.agent.session import Session, SessionManager
from smartclaw.agent.unified_execution import UnifiedExecutionEngine
from smartclaw.agent.tools import ToolRegistry, get_tool_registry
from smartclaw.agent.tools.builtin_registration import register_builtin_tools
from smartclaw.console import agent_event, debug, error, info, warning

# 工具模块会在导入时自动注册 exec/read_file/write_file 工具
from smartclaw.interfaces import (
    AgentConfig,
    AgentStatus,
    ChannelType,
)
from smartclaw.llm.base import (
    LLMProvider,
    Message as LLMMessage,
    normalize_agent_llm_dict,
    resolved_model_name_from_llm_dict,
)

# LLM 相关导入
from smartclaw.llm.registry import get_llm_registry
from smartclaw.memory.manager import MemoryManager
from smartclaw.memory.context_helpers import compact_prefix_from_memory_context
from smartclaw.memory.session_maintainer import (
    MEMORY_MAINT_DEBOUNCE_SEC,
    run_post_turn_memory_maintenance,
    should_run_session_summary,
)
# EventBus 在 start() 中按需 from smartclaw.core.event_bus import EventBus，见 platform.event_bus_enabled
from smartclaw.core.subagent_spawn import SubagentSpawner, SpawnConfig, SpawnResult
from smartclaw.core.subagent_registry import SubagentRegistry
from smartclaw.config.loader import get_config
import smartclaw.paths as paths
import contextvars
from smartclaw.paths import default_memory_data_dir
from smartclaw.logging_utils import safe_preview
from smartclaw.skills.registry import SkillRegistry
from smartclaw.skills.watch import refresh_workspace_snapshot
from smartclaw.tenant import normalize_tenant_id, tenant_agent_key

from smartclaw.agent.runner_exec_context import runner_exec_context
from smartclaw.agent.sandbox_context import reset_runner_sandbox, set_runner_sandbox
from smartclaw.auth.tool_gate import (
    get_tool_security_context,
    reset_tool_security_context,
    resolve_feishu_roles,
    set_tool_security_context,
    ToolSecurityContext,
)

# 监控导入
from smartclaw.monitoring.metrics import record_execution_path_event, record_token_usage
from smartclaw.sandbox.base import SandboxBackend
from smartclaw.sandbox.firecracker import FirecrackerBackend
from smartclaw.sandbox.pool import WarmPool


class AgentRunner:
    """
    Agent 运行器

    负责单个 Agent 的生命周期管理和消息处理。
    """

    def __init__(
        self,
        agent_id: str,
        config: AgentConfig,
        session_manager: Optional[SessionManager] = None,
        tool_registry: Optional[ToolRegistry] = None,
        sandbox_backend: Optional[SandboxBackend] = None,
        warm_pool: Optional[WarmPool] = None,
        llm_config: Optional[dict[str, Any]] = None,
        agent_profile: Optional[dict[str, Any]] = None,
    ):
        """
        初始化 Agent 运行器

        参数:
            agent_id: Agent ID
            config: Agent 配置
            session_manager: 会话管理器
            tool_registry: 工具注册表
            sandbox_backend: 沙箱后端
            warm_pool: 预热池
            llm_config: LLM 配置（包含 api_key 等）
        """
        self.agent_id = agent_id
        self.config = config
        self.agent_profile = agent_profile or {}
        self.tenant_id = normalize_tenant_id(self.agent_profile.get("tenant_id", "default"))
        self.logical_agent_key = tenant_agent_key(self.agent_id, self.tenant_id)

        self.session_manager = session_manager or SessionManager(
            agent_id=agent_id,
            tenant_id=self.tenant_id,
        )
        self.tool_registry = tool_registry or get_tool_registry()
        # 注册所有内置工具
        register_builtin_tools()
        self.sandbox_backend = sandbox_backend

        self._status = AgentStatus.STOPPED
        self._warm_pool = warm_pool
        self._sandbox_instance_id: Optional[str] = None
        self._active_sessions: dict[str, Session] = {}

        # LLM 配置（与 agent.json / set-llm 对齐，兼容 model 别名）
        self._llm_config = normalize_agent_llm_dict(llm_config) if llm_config else {}
        self._llm_adapter_name: Optional[str] = None
        self._llm_adapter = None

        # 记忆管理器
        self._memory_manager: Optional[MemoryManager] = None

        # 事件总线：在 start() 中据 platform.event_bus_enabled 创建，此处仅占位
        self._event_bus = None

        # 子 Agent 派生器
        self._subagent_spawner: Optional[SubagentSpawner] = None

        # ReAct 推理引擎
        self._react_engine: Optional[ReActEngine] = None
        self._planner: Optional[Planner] = None
        self._deep_agent: Optional[DeepAgentsWrapper] = None
        self._executor: Optional[SimpleExecutor] = None

        # 上下文模式: "full"=完整历史, "compact"=摘要历史, "minimal"=仅当前
        # 强制使用 full 模式启用记忆
        self._context_mode = "full"
        self._skills_prompt = ""
        self._skill_runtime: Any = None
        self._last_execution_plan: Optional[dict[str, Any]] = None
        self._full_agent_config: dict[str, Any] = {}
        # 同一会话记忆维护：debounce + 锁；锁表按会话 LRU 裁剪
        self._memory_maint_locks: dict[str, asyncio.Lock] = {}
        self._memory_maint_lock_ts: dict[str, float] = {}
        self._memory_maint_debounce_tasks: dict[str, asyncio.Task] = {}
        self._workspace_resolution_snap: Optional[dict[str, Any]] = None

    @property
    def status(self) -> AgentStatus:
        """获取 Agent 状态"""
        return self._status

    def _memory_maint_lock(self, tenant_id: str, session_id: str) -> asyncio.Lock:
        key = f"{tenant_id}\x1f{session_id}"
        lock = self._memory_maint_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._memory_maint_locks[key] = lock
        self._memory_maint_lock_ts[key] = time.time()
        self._trim_memory_maint_maps()
        return lock

    def _trim_memory_maint_maps(self) -> None:
        """释放长期无会话活动的维护锁 / debounce 引用，避免字典无限增长。"""
        max_keys = 300
        stale_sec = 3600.0
        if len(self._memory_maint_locks) <= max_keys:
            return
        now = time.time()
        active = {
            f"{getattr(s, 'tenant_id', 'default')}\x1f{s.session_id}"
            for s in self._active_sessions.values()
        }
        for k in list(self._memory_maint_locks.keys()):
            if k in active:
                continue
            lock = self._memory_maint_locks[k]
            if lock.locked():
                continue
            if now - self._memory_maint_lock_ts.get(k, 0) < stale_sec:
                continue
            self._memory_maint_locks.pop(k, None)
            self._memory_maint_lock_ts.pop(k, None)
            t = self._memory_maint_debounce_tasks.pop(k, None)
            if t is not None and not t.done():
                t.cancel()

    async def start(self) -> None:
        """
        启动 Agent

        初始化沙箱实例和 LLM，准备接收消息。
        """
        if self._status == AgentStatus.RUNNING:
            return

        agent_event(f"启动 Agent: {self.agent_id}")
        self._status = AgentStatus.CREATING

        try:
            # 初始化沙箱
            if self.config.sandbox_enabled:
                await self._initialize_sandbox()

            # 初始化 LLM
            await self._initialize_llm()

            # 全局 [vision] + 当前 Agent 的 llm.vision（与 feishu_multiprocess 一致）
            self._apply_vision_service_for_current_agent(verbose=True)

            from smartclaw.agent.manager import AgentManager

            self._full_agent_config = AgentManager()._read_config(
                self.agent_id,
                tenant_id=self.tenant_id,
            ) or {}
            self.tenant_id = normalize_tenant_id(
                self._full_agent_config.get("tenant_id", self.tenant_id)
            )
            from smartclaw.agent.workspace import merge_workspace_resolution_snap

            self._workspace_resolution_snap = merge_workspace_resolution_snap(
                self._full_agent_config,
                self.agent_profile,
            )
            snap_tid = (self._workspace_resolution_snap.get("tenant_id") or "").strip()
            if snap_tid:
                self.tenant_id = normalize_tenant_id(snap_tid)
            self.logical_agent_key = tenant_agent_key(self.agent_id, self.tenant_id)
            if self.config.channel == ChannelType.FEISHU:
                self._sync_feishu_tool_credentials()

            # 初始化记忆管理器
            if self._memory_manager is None:
                self._memory_manager = MemoryManager(
                    agent_id=self.agent_id,
                    session_id="default",
                    channel="feishu",
                    user_id="default",
                    data_dir=default_memory_data_dir(self.agent_id, self.tenant_id),
                )
                self._memory_manager.tenant_id = self.tenant_id

            # 初始化事件总线（Harness L1，可由 platform.event_bus_enabled 开关）
            runtime_cfg = get_config()
            if runtime_cfg.platform.event_bus_enabled:
                from pathlib import Path as _Path
                from smartclaw.core.event_bus import EventBus

                from smartclaw.paths import get_event_bus_dir

                _eb = (runtime_cfg.platform.event_bus_dir or "").strip() or str(
                    get_event_bus_dir()
                )
                self._event_bus = EventBus(_eb)
                info(f"[Runner] EventBus 已启用: {_eb}")
            else:
                self._event_bus = None

            # 初始化子 Agent 派生器，并暴露给正式 subagent_* 工具。
            self._subagent_spawner = SubagentSpawner(
                agent_runner_factory=self._create_subagent_runner,
            )
            try:
                from smartclaw.agent.tools.subagent_tool import set_subagent_spawner

                set_subagent_spawner(self.logical_agent_key, self._subagent_spawner)
                set_subagent_spawner(self.agent_id, self._subagent_spawner)
            except Exception as ex:
                warning(f"[Runner] 注册子 Agent 派生器失败: {ex}")

            # 初始化 DirectAgent (LangGraph + TodoListMiddleware)
            if self._llm_adapter:
                runtime_config = get_config()
                from smartclaw.agent.workspace import resolve_agent_workspace_dir

                snap = self._workspace_resolution_snap
                if snap is None:
                    from smartclaw.agent.workspace import merge_workspace_resolution_snap

                    snap = merge_workspace_resolution_snap(
                        self._full_agent_config,
                        self.agent_profile,
                    )
                    self._workspace_resolution_snap = snap
                logical_id = str(snap.get("name", self.agent_id))
                ws_path = resolve_agent_workspace_dir(
                    logical_id,
                    snap,
                    runtime_config,
                    tenant_id=self.tenant_id,
                )
                ws_path.mkdir(parents=True, exist_ok=True)
                ws_str = str(ws_path)
                try:
                    from smartclaw.agent.tools.workspace_tool_loader import register_workspace_tools

                    loaded = register_workspace_tools(ws_path, registry=self.tool_registry)
                    if loaded.get("loaded") or loaded.get("skipped"):
                        info(
                            "[Runner] workspace tools loaded | "
                            f"tenant={self.tenant_id} agent={self.agent_id} "
                            f"loaded={len(loaded.get('loaded', []))} skipped={len(loaded.get('skipped', []))}"
                        )
                except Exception as ex:
                    warning(f"[Runner] workspace tools 加载失败: {ex}")
                try:
                    from smartclaw.mcp import register_mcp_tools_for_agent

                    mcp_loaded = await register_mcp_tools_for_agent(
                        self._full_agent_config or {},
                        tenant_id=self.tenant_id,
                        registry=self.tool_registry,
                    )
                    if mcp_loaded.get("loaded") or mcp_loaded.get("skipped"):
                        info(
                            "[Runner] MCP tools loaded | "
                            f"tenant={self.tenant_id} agent={self.agent_id} "
                            f"loaded={len(mcp_loaded.get('loaded', []))} "
                            f"skipped={len(mcp_loaded.get('skipped', []))}"
                        )
                except Exception as ex:
                    warning(f"[Runner] MCP tools 加载失败: {ex}")
                refresh_workspace_snapshot(ws_str, config=runtime_config)
                skill_registry = SkillRegistry(ws_str, config=runtime_config)
                self._skill_runtime = skill_registry.build()
                self._skills_prompt = self._skill_runtime.skills_prompt
                record_execution_path_event("skill_registry_build")
                if self._skill_runtime.included_keys:
                    info(f"[Runner] skills prompt injected: {len(self._skill_runtime.included_keys)} skills")

                # 获取 LLM 配置
                # 从 LLM adapter 获取配置
                adapter_config = getattr(self._llm_adapter, 'config', None)
                if adapter_config:
                    llm_base_url = getattr(adapter_config, 'base_url', 'https://open.bigmodel.cn/api/coding/paas/v4/')
                    llm_api_key = getattr(adapter_config, 'api_key', '1234567890')
                    # LLMConfig 字段为 model_name；误读 .model 会永远回落到 glm-5
                    llm_model = (
                        getattr(adapter_config, "model_name", None)
                        or getattr(adapter_config, "model", None)
                        or "glm-5"
                    )
                else:
                    llm_base_url = 'https://open.bigmodel.cn/api/coding/paas/v4/'
                    llm_api_key = '1234567890'
                    llm_model = 'glm-5'
                    warning(f"[Runner] Warning: _llm_adapter.config not found, using defaults")
                
                try:
                    display_name = (
                        self.agent_profile.get("display_name")
                        or self._llm_config.get("display_name")
                        or self.agent_id
                    )
                    compiled_prompt = ""
                    for ad in paths.get_agents_dirs():
                        cp = (
                            ad / self.tenant_id / self.agent_id / ".compiled" / "agent.compiled.json"
                            if self.tenant_id != "default"
                            else ad / self.agent_id / ".compiled" / "agent.compiled.json"
                        )
                        if cp.is_file():
                            try:
                                compiled_prompt = (
                                    json.loads(cp.read_text(encoding="utf-8")).get(
                                        "system_prompt"
                                    )
                                    or ""
                                )
                            except Exception:
                                compiled_prompt = ""
                            break
                    if compiled_prompt:
                        info(
                            f"[Runner] 已加载编译人格提示 "
                            f"({len(compiled_prompt)} 字符)，确认可运行 agent compile 更新"
                        )
                    self._deep_agent = DeepAgentsWrapper(
                        base_url=llm_base_url,
                        api_key=llm_api_key,
                        model_name=llm_model,
                        agent_name=display_name,
                        skills_prompt=self._skills_prompt,
                        workspace_dir=ws_str,
                        compiled_prompt=compiled_prompt,
                        sandbox_backend=self.sandbox_backend
                        if getattr(self, "_sandbox_instance_id", None)
                        else None,
                        sandbox_instance_id=self._sandbox_instance_id or None,
                    )
                    # 异步初始化
                    import asyncio
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        asyncio.ensure_future(self._deep_agent.initialize())
                    else:
                        loop.run_until_complete(self._deep_agent.initialize())
                    info("[Runner] DeepAgents 初始化成功")
                except Exception as e:
                    error(f"[Runner] DeepAgents 初始化失败: {e}")
                    self._deep_agent = None

                # 保留 ReActEngine 作为后备
                self._react_engine = ReActEngine(
                    subagent_spawner=self._subagent_spawner,
                    tool_registry=self.tool_registry,
                    llm_callable=self._llm_adapter.chat,
                )

            self._status = AgentStatus.RUNNING
            agent_event(f"Agent 已启动: {self.agent_id}")

        except Exception as e:
            self._status = AgentStatus.ERROR
            error(f"Agent 启动失败: {e}")
            raise

    async def stop(self) -> None:
        """
        停止 Agent

        销毁本 Runner 的沙箱实例（若有）、解除 ToolRegistry 上同实例指针、清空本会话缓存。
        """
        if self._status == AgentStatus.STOPPED:
            return

        agent_event(f"停止 Agent: {self.agent_id}")

        sid = self._sandbox_instance_id
        if sid and self.sandbox_backend:
            try:
                await self.sandbox_backend.destroy_instance(sid)
            except Exception as ex:
                warning(f"[Runner] 销毁沙箱实例失败 (agent={self.agent_id}): {ex}")
            finally:
                self._sandbox_instance_id = None
                from smartclaw.agent.tools import get_tool_registry

                get_tool_registry().clear_sandbox_context_if_match(sid)

        self._active_sessions.clear()
        self._status = AgentStatus.STOPPED
        agent_event(f"Agent 已停止: {self.agent_id}")

    def _create_subagent_runner(self, **kwargs) -> "AgentRunner":
        """创建子 Agent Runner 的工厂方法"""
        from smartclaw.agent.runner import AgentRunner
        return AgentRunner(
            agent_id=kwargs.get("agent_id", "subagent"),
            config=self.config,
            llm_config=kwargs.get("llm_config") or self._llm_config,
            agent_profile=dict(self.agent_profile),
        )

    async def spawn_subagent(
        self,
        task: str,
        session_key: str,
        model: Optional[str] = None,
    ) -> SpawnResult:
        """
        派生子 Agent 执行任务

        Args:
            task: 任务描述
            session_key: 会话 ID
            model: 模型名称（可选）

        Returns:
            SpawnResult
        """
        if not self._subagent_spawner:
            return SpawnResult(status="error", error="SubagentSpawner 未初始化")

        ctx_sub = get_tool_security_context()
        tenant_for_spawn = ctx_sub.tenant_id if ctx_sub else self.tenant_id
        config = SpawnConfig(
            task=task,
            agent_id=self.agent_id,
            model=model,
            mode="run",
            timeout_seconds=300,
            tenant_id=tenant_for_spawn,
            user_id=(ctx_sub.feishu_open_id if ctx_sub else ""),
            roles=tuple(ctx_sub.roles) if ctx_sub else (),
            integration_env=tuple(ctx_sub.integration_env) if ctx_sub else (),
        )

        return await self._subagent_spawner.spawn(
            config=config,
            requester_session_key=session_key,
            requester_agent_id=self.agent_id,
        )

    async def process_message(
        self,
        user_id: str,
        channel: ChannelType,
        content: str,
        session_id: Optional[str] = None,
        is_group: bool = False,
        tenant_id: str = "default",
    ) -> str:
        """
        处理用户消息

        参数:
            user_id: 用户 ID
            channel: 渠道类型
            content: 消息内容
            session_id: 会话 ID（可选，不提供则创建新会话）
            is_group: 是否群聊（群聊不保持会话历史）

        返回:
            Agent 响应内容
        """
        if self._status != AgentStatus.RUNNING:
            raise RuntimeError(f"Agent 状态异常: {self._status}")

        tenant_id = normalize_tenant_id(tenant_id)
        if tenant_id == "default" and self.tenant_id != "default":
            tenant_id = self.tenant_id

        # 租户级准入：限流 + 每日 token 配额（治理关闭时直接放行，零开销）。
        # 并发上限在下方主流程的 try/finally 中配对 acquire/release。
        from smartclaw.governance import get_governor

        _governor = get_governor()
        _admit = _governor.admit(tenant_id)
        if not _admit.allowed:
            info(
                "[Governance] admission denied | "
                f"tenant={tenant_id} agent={self.agent_id} user={user_id} reason={_admit.reason}"
            )
            return _admit.user_message
        # 用户级准入（纯增量）：租户放行后，再校验「同租户单用户」公平性约束。
        # 未配置用户限额或无 user_id 时立即放行，行为与历史一致。
        _admit_user = _governor.admit_user(tenant_id, user_id)
        if not _admit_user.allowed:
            info(
                "[Governance] user admission denied | "
                f"tenant={tenant_id} agent={self.agent_id} user={user_id} reason={_admit_user.reason}"
            )
            return _admit_user.user_message

        # 多 Agent 同进程时视觉为单例，每次处理前同步为当前 Agent 的合并配置
        self._apply_vision_service_for_current_agent(verbose=False)
        if channel == ChannelType.FEISHU:
            self._sync_feishu_tool_credentials()

        # 判断是否是群聊（群聊不保持会话历史）
        is_group_chat = is_group

        # 获取或创建会话
        if session_id:
            try:
                session = await self.session_manager.get(session_id)
                if session is not None and getattr(session, "tenant_id", "default") != tenant_id:
                    session = None
                if session is None:
                    # 会话不存在，创建新会话（使用传入的 session_id）
                    session = await self.session_manager.create(
                        agent_id=self.agent_id,
                        channel=channel,
                        user_id=user_id,
                        session_id=session_id,
                        tenant_id=tenant_id,
                    )
                    self._active_sessions[session.session_id] = session
            except Exception:
                # 获取失败，创建新会话
                session = await self.session_manager.create(
                    agent_id=self.agent_id,
                    channel=channel,
                    user_id=user_id,
                    tenant_id=tenant_id,
                )
                self._active_sessions[session.session_id] = session
        else:
            session = await self.session_manager.create(
                agent_id=self.agent_id,
                channel=channel,
                user_id=user_id,
                tenant_id=tenant_id,
            )
            self._active_sessions[session.session_id] = session

        # 群聊会话按 chat_id 复用：把会话「当前用户」刷新为本轮真实发言人，
        # 使记忆归属（事件 / 画像 / 个人长期记忆）始终对应「说话的人」而非会话创建者。
        # 私聊场景 session.user_id 本就等于发言人，此处为 no-op，零行为变化。
        if user_id and getattr(session, "user_id", None) != user_id:
            session.user_id = user_id

        # 标记是否加载历史
        if session.context:
            session.context["load_history"] = not is_group_chat

        # 添加用户消息到会话管理器
        await self.session_manager.add_message(
            session_id=session.session_id,
            role="user",
            content=content,
        )

        # 添加用户消息到记忆管理器
        if self._memory_manager:
            # 先设置 session_id，再添加消息
            self._memory_manager.session_id = session.session_id
            self._memory_manager.tenant_id = getattr(session, "tenant_id", tenant_id)
            info(
                "[Memory] add_message | "
                f"tenant={tenant_id} agent={self.agent_id} session={session.session_id} "
                f"user={session.user_id} role=user content_preview={safe_preview(content, 80)!r}"
            )
            self._memory_manager.add_message("user", content)

        agent_event(
            "[Runner] message accepted | "
            f"tenant={tenant_id} agent={self.agent_id} session={session.session_id} "
            f"user={user_id} content_preview={safe_preview(content, 80)!r}"
        )

        cfg = get_config()
        roles = resolve_feishu_roles(tenant_id, user_id, cfg)
        env_map = (getattr(cfg.auth, "tenant_integration_env", None) or {}).get(tenant_id) or {}
        sec = ToolSecurityContext(
            tenant_id=tenant_id,
            feishu_open_id=user_id,
            roles=tuple(roles),
            agent_id=self.agent_id,
            session_id=session.session_id,
            integration_env=tuple(sorted(env_map.items())),
        )
        tok = set_tool_security_context(sec)
        sandbox_tok: Any | None = None
        if self.sandbox_backend and self._sandbox_instance_id:
            sandbox_tok = set_runner_sandbox(self.sandbox_backend, self._sandbox_instance_id)
        _conc_acquired = False
        _conc_user_acquired = False
        try:
            # 租户级并发上限：占用一个在途槽，离开时在 finally 释放。
            _conc = _governor.acquire(tenant_id)
            if not _conc.allowed:
                info(
                    "[Governance] concurrency denied | "
                    f"tenant={tenant_id} agent={self.agent_id} user={user_id}"
                )
                return _conc.user_message
            _conc_acquired = True

            # 用户级并发上限（纯增量）：失败需回滚已占的租户槽，避免泄漏。
            _conc_user = _governor.acquire_user(tenant_id, user_id)
            if not _conc_user.allowed:
                _governor.release(tenant_id)
                _conc_acquired = False
                info(
                    "[Governance] user concurrency denied | "
                    f"tenant={tenant_id} agent={self.agent_id} user={user_id}"
                )
                return _conc_user.user_message
            _conc_user_acquired = True

            preflight = preflight_capabilities(content, sec)
            if not preflight.allowed:
                response = preflight.reply
                info(
                    "[Runner] capability preflight denied | "
                    f"tenant={tenant_id} agent={self.agent_id} session={session.session_id} "
                    f"user={user_id} tools={preflight.required_tools} "
                    f"reasons={preflight.denied_reasons}"
                )
                await self.session_manager.add_message(
                    session_id=session.session_id,
                    role="assistant",
                    content=response,
                )
                if self._memory_manager:
                    self._memory_manager.add_message("assistant", response)
                return response

            response = await self._execute_loop(session)

            # 确保返回是字符串（提取 LLMResponse.content）
            if hasattr(response, "content"):
                response = response.content
            elif not isinstance(response, str):
                response = str(response)

            # 添加助手消息到会话管理器
            await self.session_manager.add_message(
                session_id=session.session_id,
                role="assistant",
                content=response,
            )

            # 添加助手消息到记忆管理器；摘要/事件维护后台执行，避免阻塞用户收消息
            if self._memory_manager:
                self._memory_manager.add_message("assistant", response)
                _mm = self._memory_manager
                _ad = self._llm_adapter_name
                _sid = session.session_id
                _tid = getattr(session, "tenant_id", tenant_id)
                _uid = session.user_id
                _load = session.context.get("load_history", True)
                _aid = self.agent_id
                _maint_lock = self._memory_maint_lock(_tid, _sid)
                _maint_key = f"{_tid}\x1f{_sid}"
                _skip_maint_debounce = False
                if _mm and _ad and _load:
                    _skip_maint_debounce = should_run_session_summary(
                        _mm._store.get_message_count(_sid, tenant_id=_tid),
                        _mm._store.get_latest_summary(_sid, tenant_id=_tid),
                    )

                async def _debounced_maint() -> None:
                    if not _skip_maint_debounce:
                        try:
                            await asyncio.sleep(MEMORY_MAINT_DEBOUNCE_SEC)
                        except asyncio.CancelledError:
                            return
                    async with _maint_lock:
                        try:
                            await run_post_turn_memory_maintenance(
                                memory_manager=_mm,
                                adapter_name=_ad,
                                agent_id=_aid,
                                session_id=_sid,
                                tenant_id=_tid,
                                user_id=_uid,
                                load_history=_load,
                            )
                        except Exception as ex:
                            error(f"[memory] 后台记忆维护失败: {ex}")

                prior = self._memory_maint_debounce_tasks.pop(_maint_key, None)
                if prior is not None and not prior.done():
                    prior.cancel()
                self._memory_maint_debounce_tasks[_maint_key] = asyncio.create_task(
                    _debounced_maint()
                )

            info(
                "[Runner] message completed | "
                f"tenant={tenant_id} agent={self.agent_id} session={session.session_id} "
                f"response_type={type(response).__name__} response_preview={safe_preview(response, 100)!r}"
            )
            return response

        except Exception as e:
            error(f"处理消息失败: {e}")

            await self.session_manager.add_message(
                session_id=session.session_id,
                role="assistant",
                content=f"处理消息时发生错误: {e}",
            )

            raise
        finally:
            if _conc_user_acquired:
                _governor.release_user(tenant_id, user_id)
            if _conc_acquired:
                _governor.release(tenant_id)
            if sandbox_tok is not None:
                reset_runner_sandbox(sandbox_tok)
            reset_tool_security_context(tok)

    def _resolve_agent_workspace_dir_early(self) -> Path:
        """
        在 ``start()`` 早期解析 Agent 工作区根路径（与 DeepAgents / workspace 模块一致）。

        Docker 沙箱需在 ``create_instance`` 时挂载宿主目录；必须在 ``_initialize_sandbox`` 中完成，
        且不得使用 ``/root/smartclaw_workspace`` 等非当前用户可写路径。
        """
        from smartclaw.agent.manager import AgentManager
        from smartclaw.agent.workspace import (
            merge_workspace_resolution_snap,
            resolve_agent_workspace_dir,
        )

        full = AgentManager()._read_config(self.agent_id, tenant_id=self.tenant_id) or {}
        self._full_agent_config = full
        snap = merge_workspace_resolution_snap(full, self.agent_profile)
        self._workspace_resolution_snap = snap
        snap_tid = (snap.get("tenant_id") or "").strip()
        if snap_tid:
            self.tenant_id = normalize_tenant_id(snap_tid)
        logical_id = str(snap.get("name", self.agent_id))
        ws_path = resolve_agent_workspace_dir(
            logical_id,
            snap,
            get_config(),
            tenant_id=self.tenant_id,
        )
        return ws_path

    async def _initialize_sandbox(self) -> None:
        """初始化沙箱实例"""
        if not self.sandbox_backend:
            # 根据配置选择沙箱后端
            from smartclaw.interfaces import SandboxBackend
            from smartclaw.sandbox import DockerSandboxBackend
            
            backend_type = getattr(self.config, 'sandbox_backend_type', SandboxBackend.FIRECRACKER)
            
            if backend_type == SandboxBackend.DOCKER:
                info("使用 Docker 沙箱后端")
                self.sandbox_backend = DockerSandboxBackend()
            else:
                # 默认使用 Firecracker
                self.sandbox_backend = FirecrackerBackend()
            
            await self.sandbox_backend.initialize()

        create_kw: dict[str, Any] = {
            "agent_id": self.agent_id,
            "memory_mb": self.config.sandbox_memory_mb,
            "cpu_count": self.config.sandbox_cpu_count,
        }
        if getattr(self.sandbox_backend, "backend_type", None) == "docker":
            ws_resolved = self._resolve_agent_workspace_dir_early()
            create_kw["host_workspace_dir"] = str(ws_resolved)
            info(
                f"[Runner] Docker 沙箱将挂载宿主工作区: {ws_resolved} -> 容器内 /root/workspace"
            )

        # 从预热池获取或创建实例
        if self._warm_pool:
            instance = await self._warm_pool.claim(self.agent_id)
            self._sandbox_instance_id = instance.instance_id
        else:
            instance = await self.sandbox_backend.create_instance(**create_kw)
            self._sandbox_instance_id = instance.instance_id

        info(f"沙箱实例已创建: {self._sandbox_instance_id}")

        # 不再写入 ToolRegistry 全局沙箱指针：同进程多 Agent 会串实例。
        # exec / 沙箱路由改由 asyncio 任务级 ContextVar（见 sandbox_context）在 process_message 内绑定。

    def _apply_vision_service_for_current_agent(self, *, verbose: bool) -> None:
        """Apply tenant-aware vision config plus current agent llm.vision."""
        from smartclaw.config.loader import tenant_vision_config
        from smartclaw.vision.service import configure_vision_for_merged_llm

        cfg = get_config()
        configure_vision_for_merged_llm(
            self._llm_config,
            tenant_vision_config(cfg, self.tenant_id),
            log_tag=self.agent_id,
            verbose=verbose,
        )

    def _sync_feishu_tool_credentials(self) -> None:
        """
        为 create_feishu_doc 等飞书 OpenAPI 工具注入当前 Agent 的 app_id / app_secret。

        feishu_doc_tool 使用进程级全局凭证；WebSocket 多 Agent 同进程时必须在每次处理
        飞书消息前按 agent_id 刷新，与 feishu_multiprocess Worker 内 set_feishu_credentials 对齐。
        """
        if self.config.channel != ChannelType.FEISHU:
            return
        try:
            from smartclaw.agent.manager import AgentManager
            from smartclaw.agent.tools.feishu_doc_tool import set_feishu_credentials

            raw = AgentManager()._read_config(self.agent_id, tenant_id=self.tenant_id)
            if not raw:
                return
            feishu_cfg = raw.get("feishu") or {}
            app_id = str(feishu_cfg.get("app_id") or "").strip()
            app_secret = feishu_cfg.get("app_secret") or ""
            if isinstance(app_secret, str) and app_secret.startswith("ENC:"):
                app_secret = AgentManager()._decrypt(app_secret[4:])
            app_secret = str(app_secret or "").strip()
            if app_id and app_secret:
                set_feishu_credentials(app_id, app_secret)
        except Exception as e:
            warning(f"[Runner] 同步飞书文档工具凭证失败 agent={self.agent_id}: {e}")

    async def _initialize_llm(self) -> None:
        """初始化 LLM 适配器"""
        if not self._llm_config:
            warning("未配置 LLM，将使用模拟响应")
            return

        from smartclaw.llm import LLMConfig

        # 解析 provider
        provider_str = self._llm_config.get("provider", "openai")
        try:
            provider = LLMProvider(provider_str)
        except ValueError:
            provider = LLMProvider.OPENAI

        # 创建 LLM 配置
        llm_config = LLMConfig(
            provider=provider,
            model_name=resolved_model_name_from_llm_dict(self._llm_config, "gpt-4"),
            api_key=self._llm_config.get("api_key"),
            base_url=self._llm_config.get("base_url"),
            temperature=self._llm_config.get("temperature", 0.7),
            max_tokens=self._llm_config.get("max_tokens", 4096),
        )

        # 注册到全局注册表
        registry = get_llm_registry()
        adapter_name = f"{self.agent_id}-llm"
        registry.register(adapter_name, llm_config)
        self._llm_adapter_name = adapter_name
        self._llm_adapter = registry.get(adapter_name)

        info(f"LLM 已配置: {provider.value}/{llm_config.model_name}")

    async def _execute_loop(self, session: Session, depth: int = 0, max_depth: int = 10) -> str:
        """执行主循环：默认走 UnifiedExecutionEngine；关闭 execution.use_unified_engine 时回退内联实现。"""
        cfg = get_config()
        if cfg.execution.use_unified_engine:
            return await UnifiedExecutionEngine(self).execute_turn(session, depth=depth, max_depth=max_depth)
        return await self._legacy_execute_loop(session, depth=depth, max_depth=max_depth)

    async def _legacy_execute_loop(self, session: Session, depth: int = 0, max_depth: int = 10) -> str:
        """
        内联执行循环（应急回滚路径，与引入统一引擎前的行为一致）。
        """
        # 防止无限递归
        if depth >= max_depth:
            warning(f"Agent 执行达到最大递归深度 ({max_depth})，停止执行")
            return "工具执行已达最大次数，停止执行。"

        # 获取会话历史（群聊模式不加载历史）
        messages = await self.session_manager.get_messages(session.session_id)
        if not session.context.get("load_history", True):
            # 群聊模式：只保留最新消息（当前用户消息）
            if messages:
                messages = [messages[-1]]

        # 构建消息历史
        history = self._build_message_history(messages)
        if self._skills_prompt:
            history = [{"role": "system", "content": self._skills_prompt}] + history

        user_message = ""
        for msg in reversed(history):
            if msg.get("role") == "user":
                user_message = msg.get("content", "") or ""
                break

        # 注入记忆上下文（根据上下文模式）
        debug(
            "[Memory] inject start (legacy) | "
            f"context_mode={self._context_mode} "
            f"memory_manager_set={self._memory_manager is not None} "
            f"session_id={session.session_id!r}"
        )
        if self._memory_manager and self._context_mode != "minimal":
            self._memory_manager.session_id = session.session_id
            self._memory_manager.user_id = session.user_id
            if hasattr(self._memory_manager, "tenant_id"):
                self._memory_manager.tenant_id = getattr(session, "tenant_id", "default")

            um_prev = safe_preview(user_message or "", 100)
            debug(
                "[Memory] fts prelude (legacy) | "
                f"fts_ready={self._memory_manager.fts_ready} "
                f"retrieval_preview={um_prev!r}"
            )
            memory_context = self._memory_manager.get_context_for_llm(
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
                "[Memory] inject done (legacy) | "
                f"blocks={len(memory_context) if memory_context else 0} "
                f"has_retrieval={has_retrieval}"
            )
            if memory_context:
                if self._context_mode == "full":
                    history = memory_context + history
                elif self._context_mode == "compact":
                    prefix = compact_prefix_from_memory_context(memory_context)
                    if prefix:
                        history = prefix + history

        ex = get_config().execution
        # 使用 DeepAgents (LangGraph + TodoListMiddleware)
        if ex.deepagents_enabled and self._deep_agent and depth == 0:
            try:
                history_for_deepagents = clip_history_for_deepagents(history, self._skills_prompt)
                if len(history_for_deepagents) < len(history):
                    info(f"[Runner] 历史消息已压缩: {len(history)} -> {len(history_for_deepagents)} 条")
                info(f"[Runner] 使用 DeepAgents 执行，记忆历史 {len(history_for_deepagents)} 条")

                import asyncio
                from functools import partial

                loop = asyncio.get_event_loop()
                from smartclaw.agent.tools.exec_context import (
                    reset_agent_config_for_exec,
                    reset_workspace_resolution_snap,
                    set_agent_config_for_exec,
                    set_workspace_resolution_snap,
                )

                ws_snap_inner = getattr(self, "_workspace_resolution_snap", None)
                ws_tok_exec = set_workspace_resolution_snap(
                    ws_snap_inner if isinstance(ws_snap_inner, dict) else None
                )
                exec_cfg_tok = set_agent_config_for_exec(self._full_agent_config or None)
                try:
                    ctx = contextvars.copy_context()
                    result = await loop.run_in_executor(
                        None,
                        lambda c=ctx: c.run(
                            partial(
                                self._deep_agent.run,
                                history_for_deepagents,
                                thread_id=session.session_id,
                            )
                        ),
                    )
                finally:
                    reset_agent_config_for_exec(exec_cfg_tok)
                    reset_workspace_resolution_snap(ws_tok_exec)

                info(
                    "[Runner] DeepAgents completed | "
                    f"tenant={getattr(session, 'tenant_id', self.tenant_id)} "
                    f"agent={self.agent_id} session={session.session_id} "
                    f"result_preview={safe_preview(result, 120)!r}"
                )
                return result
            except Exception as e:
                error(f"[Runner] DeepAgents 执行失败: {e}，使用原有引擎")

        # 使用 ReAct 引擎执行
        if ex.react_fallback_enabled and self._react_engine and self._llm_adapter:
            try:
                # 获取用户消息（最后一条用户消息）
                user_message = ""
                for msg in reversed(history):
                    if msg.get("role") == "user":
                        user_message = msg.get("content", "")
                        break

                async with runner_exec_context(self):
                    result: ExecutionResult = await self._react_engine.execute(
                        user_message=user_message,
                        context=history,  # 包含完整上下文
                        system_prompt=self._skills_prompt,
                        session_id=session.session_id,  # 传递 session_id
                    )

                info(
                    "[Runner] ReAct completed | "
                    f"result_type={type(result).__name__} "
                    f"final_response_type={type(result.final_response).__name__ if hasattr(result, 'final_response') else 'N/A'}"
                )
                if hasattr(result, 'final_response'):
                    info(
                        "[Runner] ReAct response preview | "
                        f"{safe_preview(result.final_response, 120)!r}"
                    )

                # 记录执行步骤
                for step in result.steps:
                    if step.tool_name:
                        await self.session_manager.add_message(
                            session_id=session.session_id,
                            role="assistant",
                            content=step.content,
                            tool_name=step.tool_name,
                        )

                return result.final_response

            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                error(f"ReAct 执行失败: {e}\n完整 traceback:\n{tb}")
                # 降级到普通 LLM 调用

        if not ex.llm_tool_fallback_enabled:
            return "执行路径已禁用 LLM+工具降级。"

        # 降级：使用普通 LLM 调用
        response = await self._call_llm(history)

        tool_calls = response.get("tool_calls", [])

        if tool_calls:
            # 检查重复 tool_calls（防止 LLM 返回重复调用）
            seen_calls = set()
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
                tool_name = tool_call.get("name") or tool_call.get("function", {}).get(
                    "name", ""
                )
                tool_params = tool_call.get("parameters") or tool_call.get(
                    "function", {}
                ).get("arguments", {})

                await self.session_manager.add_message(
                    session_id=session.session_id,
                    role="assistant",
                    content="",
                    tool_name=tool_name,
                    tool_call_id=tool_call.get("id", ""),
                )

                tool_result = await self._execute_tool(
                    tool_name=tool_name,
                    parameters=tool_params,
                )

                await self.session_manager.add_message(
                    session_id=session.session_id,
                    role="tool",
                    content=json.dumps(
                        tool_result.result if tool_result.success else tool_result.error
                    ),
                    tool_name=tool_name,
                    tool_call_id=tool_call.get("id", ""),
                )

            return await self._legacy_execute_loop(session, depth=depth + 1)

        # 确保返回字符串
        if hasattr(response, "content"):
            return response.content
        elif isinstance(response, dict):
            return response.get("content", "")
        else:
            return str(response)

    async def _call_llm(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """
        调用 LLM

        参数:
            messages: 消息历史

        返回:
            LLM 响应字典
        """
        if not self._llm_adapter_name:
            # 模拟响应
            return {
                "content": "这是一个模拟的响应。请配置 API Key 以使用真实 LLM。",
                "tool_calls": None,
            }

        try:
            # 转换消息格式
            llm_messages = []
            for msg in messages:
                llm_msg = LLMMessage(role=msg["role"], content=msg.get("content"))
                llm_messages.append(llm_msg)

            # 调用 LLM
            import time

            start_time = time.time()

            registry = get_llm_registry()
            response = await registry.chat(
                messages=llm_messages,
                adapter_name=self._llm_adapter_name,
                tools=self.tool_registry.get_openai_tools(),
            )

            latency_ms = int((time.time() - start_time) * 1000)

            # 记录 token 使用（带 tenant_id：多租户计量 + 配额累计）。
            # 用户级归属（纯增量）：从安全上下文取当前飞书 open_id（runner 早期已 set），
            # 无上下文时为空串 → 仅按租户计量，行为与历史一致。
            _sec_ctx = get_tool_security_context()
            _user_open_id = _sec_ctx.feishu_open_id if _sec_ctx else ""
            record_token_usage(
                agent_id=self.agent_id,
                provider=response.provider.value,
                model=response.model,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                latency_ms=latency_ms,
                tenant_id=self.tenant_id,
                user_open_id=_user_open_id,
            )

            # 转换响应
            result = {
                "content": response.content,
                "tool_calls": (
                    [tc.to_dict() for tc in response.tool_calls]
                    if response.tool_calls
                    else None
                ),
            }

            info(
                "[Runner] LLM response | "
                f"tokens={response.total_tokens} latency_ms={latency_ms} "
                f"content_type={type(result['content']).__name__} "
                f"content_preview={safe_preview(result['content'], 100)!r}"
            )

            return result

        except Exception as e:
            error(f"LLM 调用失败: {e}")
            return {
                "content": f"LLM 调用失败: {e}",
                "tool_calls": None,
            }

    def _build_message_history(
        self,
        messages: list[Any],
    ) -> list[dict[str, Any]]:
        """
        构建消息历史

        参数:
            messages: 消息列表

        返回:
            格式化的消息历史
        """
        history = []

        for msg in messages:
            if msg.role == "user":
                history.append({"role": "user", "content": msg.content})
            elif msg.role == "assistant":
                if msg.tool_name:
                    # 工具调用消息
                    history.append(
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": msg.tool_call_id,
                                    "type": "function",
                                    "function": {
                                        "name": msg.tool_name,
                                        "arguments": msg.content,
                                    },
                                }
                            ],
                        }
                    )
                else:
                    history.append({"role": "assistant", "content": msg.content})
            elif msg.role == "tool":
                history.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.tool_call_id,
                        "content": msg.content,
                    }
                )
            elif msg.role == "system":
                history.append({"role": "system", "content": msg.content})

        return history

    async def _execute_tool(
        self,
        tool_name: str,
        parameters: dict[str, Any],
    ) -> Any:
        """
        执行工具

        参数:
            tool_name: 工具名称
            parameters: 工具参数

        返回:
            工具执行结果
        """
        from smartclaw.skills.storage import record_event

        from smartclaw.agent.tools.exec_context import (
            reset_agent_config_for_exec,
            reset_workspace_resolution_snap,
            set_agent_config_for_exec,
            set_workspace_resolution_snap,
        )

        ws_snap = getattr(self, "_workspace_resolution_snap", None)
        ws_tok = set_workspace_resolution_snap(ws_snap if isinstance(ws_snap, dict) else None)
        token = set_agent_config_for_exec(self._full_agent_config or None)
        try:
            result = await self.tool_registry.execute(tool_name, parameters)
        finally:
            reset_agent_config_for_exec(token)
            reset_workspace_resolution_snap(ws_tok)
        event = "invoke" if getattr(result, "success", False) else "error"
        record_event(event, tool_name, {"tool_name": tool_name, "success": getattr(result, "success", False)})
        return result

    def set_api_key(self, api_key: str) -> None:
        """
        设置 API Key

        参数:
            api_key: API Key
        """
        if self._llm_config:
            self._llm_config["api_key"] = api_key

