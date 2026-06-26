import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from smartclaw.auth.tool_gate import (
    ToolSecurityContext,
    reset_tool_security_context,
    set_tool_security_context,
)
from smartclaw.core.subagent_registry import SubagentRegistry, SubagentRun, SubagentStatus


@dataclass
class SpawnConfig:
    """派生配置"""
    task: str
    agent_id: Optional[str] = None
    model: Optional[str] = None
    mode: str = "run"
    timeout_seconds: Optional[int] = None
    tenant_id: str = "default"
    user_id: str = ""
    child_session_key: str = ""
    roles: tuple[str, ...] = field(default_factory=tuple)
    integration_env: tuple[tuple[str, str], ...] = field(default_factory=tuple)


@dataclass
class SpawnResult:
    """派生结果"""
    status: str  # "accepted" | "forbidden" | "error"
    job_id: Optional[str] = None
    note: Optional[str] = None
    error: Optional[str] = None


class SubagentSpawner:
    """
    简化的子 Agent 分派器
    
    核心变化：
    - 不再依赖 EventBus
    - 使用简单的 Registry 追踪状态
    - launch() 立即返回 job_id
    - check() 查询状态和结果
    """

    def __init__(
        self,
        agent_runner_factory: Callable,
        max_concurrent: int = 5,
        registry: SubagentRegistry | None = None,
        state_dir: str | Path | None = None,
    ):
        self.agent_runner_factory = agent_runner_factory
        self.max_concurrent = max_concurrent
        self._registry = registry or SubagentRegistry(state_dir=state_dir)
        self._running_count = 0

    @property
    def registry(self) -> SubagentRegistry:
        return self._registry

    async def spawn(
        self,
        config: SpawnConfig,
        requester_session_key: str = "",
        requester_agent_id: str = "",
    ) -> SpawnResult:
        return await self.launch(config, requester_session_key, requester_agent_id)

    async def launch(
        self,
        config: SpawnConfig,
        requester_session_key: str = "",
        requester_agent_id: str = "",
    ) -> SpawnResult:
        """
        启动子 Agent，立即返回 job_id
        
        Args:
            config: 派生配置
        
        Returns:
            SpawnResult with job_id
        """
        # 检查并发限制
        if self._running_count >= self.max_concurrent:
            return SpawnResult(
                status="forbidden",
                error=f"达到最大并发数 ({self.max_concurrent})"
            )
        
        run = SubagentRun(
            task=config.task,
            agent_id=config.agent_id or requester_agent_id or "default",
            model=config.model,
            mode=config.mode,
            timeout_seconds=config.timeout_seconds,
            requester_session_key=requester_session_key,
            child_session_key="",
        )
        run_id = self._registry.register(run)
        
        # 异步执行
        self._running_count += 1
        asyncio.create_task(self._run(run_id, config))
        
        return SpawnResult(
            status="accepted",
            job_id=run_id,
            note=f"后台子 Agent 已启动，job_id: {run_id}"
        )

    async def _run(self, run_id: str, config: SpawnConfig):
        """执行子 Agent"""
        run = self._registry.get(run_id)
        if not run:
            return
        runner = None
        
        try:
            self._registry.mark_started(run_id)
            child_session = f"subagent:{run_id}"
            config.child_session_key = child_session
            self._registry.update(run_id, child_session_key=child_session)
            
            # 创建 Agent Runner
            runner = self.agent_runner_factory(
                agent_id=config.agent_id or "default",
                session_key=child_session,
                model=config.model,
            )
            
            # 启动 Runner
            await runner.start()
            
            # 执行命令
            result_text = await asyncio.wait_for(
                self._execute_command(runner, config),
                timeout=config.timeout_seconds,
            ) if config.timeout_seconds else await self._execute_command(runner, config)
            
            # 标记成功
            self._registry.mark_completed(run_id, result_text)
            
        except asyncio.TimeoutError:
            self._registry.update(
                run_id,
                status=SubagentStatus.TIMEOUT,
                completed_at=datetime.now(),
                error="执行超时",
            )
            
        except Exception as e:
            self._registry.mark_failed(run_id, str(e))
            
        finally:
            self._running_count -= 1
            if runner is not None:
                try:
                    await runner.stop()
                except Exception:
                    pass

    async def _execute_command(self, runner: Any, config: SpawnConfig) -> str:
        """执行命令"""
        import re
        
        # 从 task 中提取命令
        command = None
        task = config.task
        
        if "执行命令:" in task:
            match = re.search(r'执行命令:\s*(.+)', task)
            if match:
                command = match.group(1).strip()
        
        if not command:
            command = task
        
        tok = set_tool_security_context(
            ToolSecurityContext(
                tenant_id=config.tenant_id,
                feishu_open_id=config.user_id,
                roles=config.roles or ("default",),
                agent_id=config.agent_id or getattr(runner, "agent_id", ""),
                session_id=config.child_session_key or "",
                integration_env=config.integration_env,
            )
        )
        from smartclaw.agent.runner_exec_context import runner_exec_context

        try:
            async with runner_exec_context(runner):
                tool_result = await runner.tool_registry.execute(
                    name="exec",
                    parameters={"command": command},
                )
        finally:
            reset_tool_security_context(tok)
        
        if tool_result.success:
            return str(tool_result.result)
        else:
            return f"错误: {tool_result.error}"

    async def check(self, job_id: str) -> Optional[SubagentRun]:
        """
        检查任务状态
        
        Args:
            job_id: 任务 ID
        
        Returns:
            SubagentRun 或 None
        """
        return self._registry.get(job_id)

    async def cancel(self, job_id: str) -> bool:
        """取消任务"""
        run = self._registry.get(job_id)
        if not run:
            return False
        
        if run.status in {SubagentStatus.COMPLETED, SubagentStatus.FAILED, SubagentStatus.KILLED, SubagentStatus.TIMEOUT}:
            return False
        
        self._registry.mark_killed(job_id)
        return True
