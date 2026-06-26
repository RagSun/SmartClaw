"""
Firecracker 沙箱 DeepAgents 后端

继承 LocalShellBackend，但 execute() 路由到 Firecracker 沙箱。
文件操作依然走 LocalShellBackend（映射了工作区）。
沙箱失败时不允许回退本地，必须返回错误给上层。
"""

import asyncio
import uuid
from pathlib import Path

from deepagents.backends import LocalShellBackend
from deepagents.backends.protocol import EditResult, ExecuteResponse, WriteResult

from smartclaw.agent.deepagents_shell_gate import (
    gate_deepagents_execute,
    gate_deepagents_file_write,
)
from smartclaw.agent.bg_execute import (
    bg_execute_enabled,
    command_already_backgrounded,
    compact_background_url_suffix,
    is_probably_long_running_server,
    maybe_unescape_heredoc_newlines,
    wrap_command_linux_container,
)
from smartclaw.agent.bg_probe import (
    bg_probe_budget_seconds,
    bg_probe_enabled,
    build_sandbox_smoke_shell,
    build_sandbox_tcp_probe_command,
    infer_listen_for_probe,
    parse_bg_smoke_line,
    parse_sandbox_probe_stdout,
)
from smartclaw.agent.bg_probe_decl import resolve_bg_probe_decl
from smartclaw.agent.runtime_trace import is_deepagents_verbose
from smartclaw.agent.tools.exec_context import get_agent_config_for_exec
from smartclaw.agent.tools.loop_detector import TOOL_DEEPAGENTS_SHELL, get_loop_detector
from smartclaw.console import error, info

# 沙箱内路径
SANDBOX_WORKSPACE = "/root/smartclaw_workspace"
# 宿主机路径
HOST_WORKSPACE = "./smartclaw_workspace"


class FirecrackerDeepAgentsBackend(LocalShellBackend):
    """
    深度集成 Firecracker 沙箱的 DeepAgents 后端
    
    文件读写依然走 LocalShellBackend（映射了工作区），
    但危险的 Shell 执行 (execute) 强制路由到 Firecracker 沙箱。
    沙箱失败时不允许回退本地。
    """

    def __init__(
        self,
        root_dir: str,
        sandbox_backend,
        instance_id: str,
        env: dict = None,
        **kwargs
    ):
        # 使用沙箱路径初始化（供文件操作使用）
        super().__init__(root_dir=root_dir, env=env, virtual_mode=True, **kwargs)
        self.sandbox_backend = sandbox_backend
        self.instance_id = instance_id
        if is_deepagents_verbose():
            info(f"[FirecrackerDeepAgentsBackend] 初始化，沙箱实例: {instance_id}")

    def write(self, file_path: str, content: str):
        denied = gate_deepagents_file_write("write_file")
        if denied:
            return WriteResult(error=f"DeepAgents write_file 被权限门禁拒绝：{denied}")
        return super().write(file_path, content)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ):
        denied = gate_deepagents_file_write("edit_file")
        if denied:
            return EditResult(error=f"DeepAgents edit_file 被权限门禁拒绝：{denied}")
        return super().edit(file_path, old_string, new_string, replace_all)

    async def _do_sandbox_execute(self, command: str, timeout_ms: int):
        """异步执行沙箱命令"""
        return await self.sandbox_backend.execute(
            instance_id=self.instance_id,
            command=command,
            timeout_ms=timeout_ms,
        )

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """同步执行 - 强制沙箱，不允许本地回退"""
        if is_deepagents_verbose():
            info(f"[FC-Backend.execute] 实例={self.instance_id}, cmd={command[:50]}...")

        det = get_loop_detector()

        def _finish(resp: ExecuteResponse) -> ExecuteResponse:
            if det and isinstance(command, str):
                out = (resp.output or "") + (
                    ("\n" + resp.stderr) if getattr(resp, "stderr", None) else ""
                )
                det.record(
                    command,
                    TOOL_DEEPAGENTS_SHELL,
                    resp.exit_code == 0,
                    out[:500],
                )
            return resp

        if det:
            lr = det.check_proposed(TOOL_DEEPAGENTS_SHELL, command)
            if lr.is_loop:
                return ExecuteResponse(
                    output=lr.suggested_action,
                    exit_code=1,
                    truncated=False,
                )

        gated = gate_deepagents_execute(command, workspace_root=getattr(self, "cwd", None))
        if gated is not None:
            return _finish(gated)

        # heredoc 还原（与 Docker 后端一致）：避免字面 \n 把 cat << EOF 弄崩
        command = maybe_unescape_heredoc_newlines(command)

        timeout_ms = (timeout or 120) * 1000
        exec_command = command
        if (
            bg_execute_enabled()
            and is_probably_long_running_server(command)
            and not command_already_backgrounded(command)
        ):
            log_rel = f".smartclaw_bg/bg_{uuid.uuid4().hex[:12]}.log"
            exec_command = wrap_command_linux_container(
                command,
                workspace_posix=SANDBOX_WORKSPACE,
                log_relpath=log_rel,
            )

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(
                    self._do_sandbox_execute(exec_command, timeout_ms)
                )
                if is_deepagents_verbose():
                    info(
                        f"[FC-Backend.execute] 沙箱执行完成: exit={result.exit_code}"
                    )
                out = result.stdout + (
                    "\n" + result.stderr if result.stderr else ""
                )
                if exec_command != command:
                    out = f"{out.rstrip()}\n{compact_background_url_suffix(command)}"
                    decl = resolve_bg_probe_decl(
                        agent_cfg=get_agent_config_for_exec(),
                        workspace_root=Path(str(self.cwd)),
                    )
                    if (
                        result.exit_code == 0
                        and bg_probe_enabled()
                        and decl.tcp_probe
                    ):
                        _bd = bg_probe_budget_seconds()
                        _inf = infer_listen_for_probe(command) if _bd > 0 else None
                        if _inf and _bd > 0:
                            _probe_cmd = build_sandbox_tcp_probe_command(
                                _inf[1], _bd
                            )
                            _probe_ms = min(120_000, int(_bd * 1000) + 15_000)
                            try:
                                _pr = loop.run_until_complete(
                                    self._do_sandbox_execute(
                                        _probe_cmd, _probe_ms
                                    )
                                )
                                _blob = (_pr.stdout or "") + "\n" + (
                                    _pr.stderr or ""
                                )
                                _pline = parse_sandbox_probe_stdout(_blob)
                                if _pline:
                                    out = f"{out.rstrip()}\n[bg_probe] {_pline}"
                                else:
                                    out = (
                                        f"{out.rstrip()}\n[bg_probe] "
                                        f"sandbox_probe_exit={_pr.exit_code}"
                                    )
                            except Exception:
                                pass
                    if (
                        result.exit_code == 0
                        and decl.after_smoke.strip()
                    ):
                        _scm = build_sandbox_smoke_shell(
                            decl,
                            starter_command=command,
                            workspace_posix=SANDBOX_WORKSPACE,
                        )
                        if _scm:
                            _sm_ms = min(
                                900_000,
                                int(decl.after_smoke_timeout_sec * 1000) + 10_000,
                            )
                            try:
                                _sr = loop.run_until_complete(
                                    self._do_sandbox_execute(_scm, _sm_ms)
                                )
                                _sblob = (_sr.stdout or "") + "\n" + (
                                    _sr.stderr or ""
                                )
                                _sline = parse_bg_smoke_line(_sblob)
                                if _sline:
                                    out = (
                                        f"{out.rstrip()}\n[bg_smoke] {_sline}"
                                    )
                                else:
                                    out = (
                                        f"{out.rstrip()}\n[bg_smoke] "
                                        f"exit={_sr.exit_code}"
                                    )
                            except Exception:
                                pass
            finally:
                loop.close()

            return _finish(
                ExecuteResponse(
                    output=out,
                    exit_code=result.exit_code,
                    truncated=False,
                )
            )
        except Exception as e:
            error(f"[FC-Backend.execute] 沙箱执行失败: {e}")
            return _finish(
                ExecuteResponse(
                    output="",
                    stderr=f"[FC-Backend] 沙箱执行失败: {e}",
                    exit_code=1,
                    truncated=False,
                )
            )

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """异步执行 - 强制沙箱，不允许本地回退"""
        if is_deepagents_verbose():
            info(f"[FC-Backend.aexecute] 实例={self.instance_id}, cmd={command[:50]}...")

        gated = gate_deepagents_execute(command, workspace_root=getattr(self, "cwd", None))
        if gated is not None:
            return gated

        command = maybe_unescape_heredoc_newlines(command)

        timeout_ms = (timeout or 120) * 1000
        exec_command = command
        if (
            bg_execute_enabled()
            and is_probably_long_running_server(command)
            and not command_already_backgrounded(command)
        ):
            log_rel = f".smartclaw_bg/bg_{uuid.uuid4().hex[:12]}.log"
            exec_command = wrap_command_linux_container(
                command,
                workspace_posix=SANDBOX_WORKSPACE,
                log_relpath=log_rel,
            )

        try:
            result = await self._do_sandbox_execute(exec_command, timeout_ms)
            if is_deepagents_verbose():
                info(f"[FC-Backend.aexecute] 沙箱执行完成: exit={result.exit_code}")
            out = result.stdout + ("\n" + result.stderr if result.stderr else "")
            if exec_command != command:
                out = f"{out.rstrip()}\n{compact_background_url_suffix(command)}"
                decl = resolve_bg_probe_decl(
                    agent_cfg=get_agent_config_for_exec(),
                    workspace_root=Path(str(self.cwd)),
                )
                if (
                    result.exit_code == 0
                    and bg_probe_enabled()
                    and decl.tcp_probe
                ):
                    _bd = bg_probe_budget_seconds()
                    _inf = infer_listen_for_probe(command) if _bd > 0 else None
                    if _inf and _bd > 0:
                        _probe_cmd = build_sandbox_tcp_probe_command(_inf[1], _bd)
                        _probe_ms = min(120_000, int(_bd * 1000) + 15_000)
                        try:
                            _pr = await self._do_sandbox_execute(
                                _probe_cmd, _probe_ms
                            )
                            _blob = (_pr.stdout or "") + "\n" + (_pr.stderr or "")
                            _pline = parse_sandbox_probe_stdout(_blob)
                            if _pline:
                                out = f"{out.rstrip()}\n[bg_probe] {_pline}"
                            else:
                                out = (
                                    f"{out.rstrip()}\n[bg_probe] "
                                    f"sandbox_probe_exit={_pr.exit_code}"
                                )
                        except Exception:
                            pass
                if result.exit_code == 0 and decl.after_smoke.strip():
                    _scm = build_sandbox_smoke_shell(
                        decl,
                        starter_command=command,
                        workspace_posix=SANDBOX_WORKSPACE,
                    )
                    if _scm:
                        _sm_ms = min(
                            900_000,
                            int(decl.after_smoke_timeout_sec * 1000) + 10_000,
                        )
                        try:
                            _sr = await self._do_sandbox_execute(_scm, _sm_ms)
                            _sblob = (_sr.stdout or "") + "\n" + (_sr.stderr or "")
                            _sline = parse_bg_smoke_line(_sblob)
                            if _sline:
                                out = f"{out.rstrip()}\n[bg_smoke] {_sline}"
                            else:
                                out = (
                                    f"{out.rstrip()}\n[bg_smoke] exit={_sr.exit_code}"
                                )
                        except Exception:
                            pass
            return ExecuteResponse(
                output=out,
                exit_code=result.exit_code,
                truncated=False,
            )
        except Exception as e:
            error(f"[FC-Backend.aexecute] 沙箱执行失败: {e}")
            return ExecuteResponse(
                output="",
                stderr=f"[FC-Backend] 沙箱执行失败: {e}",
                exit_code=1,
                truncated=False,
            )


__all__ = ["FirecrackerDeepAgentsBackend"]
