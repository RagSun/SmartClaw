"""
Docker DeepAgents 后端

继承 LocalShellBackend，但 execute() 路由到 Docker 容器。
文件操作依然走 LocalShellBackend（映射了工作区）。
"""

import asyncio
import re
import subprocess
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
from smartclaw.console import debug, error, info
from smartclaw.subprocess_io import SUBPROCESS_TEXT_KWARGS

# 沙箱内工作目录兜底默认（实际值取自 docker_backend.container_workspace，须与 sandbox/docker.py 挂载点一致）
DEFAULT_SANDBOX_WORKSPACE = "/workspace"
# 历史占位：宿主默认已不再强制使用此路径（参见 DockerSandboxBackend 默认目录）
HOST_WORKSPACE = "/root/smartclaw_workspace"


class DockerDeepAgentsBackend(LocalShellBackend):
    """
    深度集成 Docker 沙箱的 DeepAgents 后端
    
    文件读写依然走 LocalShellBackend（映射了工作区），
    但危险的 Shell 执行 (execute) 强制路由到 Docker 容器。
    """

    def __init__(
        self,
        root_dir: str,
        docker_backend,
        instance_id: str,
        env: dict = None,
        **kwargs
    ):
        # 使用宿主机路径初始化（供文件操作使用）
        super().__init__(root_dir=root_dir, env=env, virtual_mode=True, **kwargs)
        self.docker_backend = docker_backend
        self.instance_id = instance_id
        # 容器内工作区挂载点，须与 sandbox/docker.py 的 -v/-w 一致
        self.container_workspace = getattr(
            docker_backend, "container_workspace", DEFAULT_SANDBOX_WORKSPACE
        )
        self.container_id = None

        # 从 instance_id 获取 container_id
        if instance_id in docker_backend._instances:
            self.container_id = docker_backend._instances[instance_id].container_name

        if is_deepagents_verbose():
            info(
                f"[DockerDeepAgentsBackend] 初始化，实例: {instance_id}, "
                f"容器: {self.container_id[:12] if self.container_id else 'N/A'}"
            )

    def _host_workspace_posix(self) -> str:
        """与 write_file 一致的宿主工作区根（resolve 后 POSIX）。"""
        try:
            raw = getattr(self, "cwd", None) or getattr(self, "root_dir", None) or ""
            return Path(str(raw)).expanduser().resolve().as_posix().rstrip("/")
        except Exception:
            return ""

    def _rewrite_host_workspace_paths_for_container(self, cmd: str) -> str:
        """
        execute 经 docker exec 在容器内运行；宿主绝对路径在容器中不存在。
        将「当前 Agent 工作区根」的宿主前缀替换为挂载点 ``self.container_workspace``。
        """
        if not cmd or not isinstance(cmd, str):
            return cmd
        host_root = self._host_workspace_posix()
        if (
            not host_root
            or host_root == self.container_workspace
            or host_root not in cmd
        ):
            return cmd
        # 先替换「根目录/」与引号内路径，再替换末尾或无后缀子路径（如 cd …/ws）
        pat_path = re.escape(host_root) + r"(?=/|'|\")"
        rewritten = re.sub(pat_path, self.container_workspace, cmd)
        pat_standalone = re.escape(host_root) + r"(?=$|\s|;|\)|\||&|`)"
        rewritten = re.sub(pat_standalone, self.container_workspace, rewritten)
        if rewritten != cmd and is_deepagents_verbose():
            debug(
                "[DockerDeepAgentsBackend] execute 路径重写: 宿主前缀 "
                f"{host_root!r} -> {self.container_workspace!r}"
            )
        return rewritten

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

    async def _do_docker_execute(self, command: str, timeout_ms: int):
        """异步执行 Docker 命令"""
        return await self.docker_backend.execute(
            instance_id=self.instance_id,
            command=command,
            timeout_ms=timeout_ms,
        )

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """同步执行 - 强制 Docker 容器，不允许本地回退"""
        if is_deepagents_verbose():
            info(f"[Docker-Backend.execute] 实例={self.instance_id}, cmd={command[:50]}...")

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

        # heredoc 还原：把"被序列化成字面 \n 的 cat << 'EOF' ... EOF"恢复为真换行。
        # 不命中 heredoc 模式或包含真换行时原样返回，对正常命令零影响。
        command = maybe_unescape_heredoc_newlines(command)

        cmd_for_container = self._rewrite_host_workspace_paths_for_container(command)

        timeout_ms = (timeout or 120) * 1000
        exec_command = cmd_for_container
        if (
            bg_execute_enabled()
            and is_probably_long_running_server(command)
            and not command_already_backgrounded(command)
        ):
            log_rel = f".smartclaw_bg/bg_{uuid.uuid4().hex[:12]}.log"
            exec_command = wrap_command_linux_container(
                cmd_for_container,
                workspace_posix=self.container_workspace,
                log_relpath=log_rel,
            )

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(
                    self._do_docker_execute(exec_command, timeout_ms)
                )
                if is_deepagents_verbose():
                    info(
                        f"[Docker-Backend.execute] Docker 执行完成: exit={result.exit_code}"
                    )
                out = result.stdout + (
                    "\n" + result.stderr if result.stderr else ""
                )
                if exec_command != cmd_for_container:
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
                                    self._do_docker_execute(
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
                            workspace_posix=self.container_workspace,
                        )
                        if _scm:
                            _sm_ms = min(
                                900_000,
                                int(decl.after_smoke_timeout_sec * 1000) + 10_000,
                            )
                            try:
                                _sr = loop.run_until_complete(
                                    self._do_docker_execute(_scm, _sm_ms)
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
            error(f"[Docker-Backend.execute] Docker 执行失败: {e}")
            return _finish(
                ExecuteResponse(
                    output="",
                    stderr=f"[Docker-Backend] Docker 执行失败: {e}",
                    exit_code=1,
                    truncated=False,
                )
            )

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """异步执行 - 强制 Docker 容器，不允许本地回退"""
        if is_deepagents_verbose():
            info(f"[Docker-Backend.aexecute] 实例={self.instance_id}, cmd={command[:50]}...")

        gated = gate_deepagents_execute(command, workspace_root=getattr(self, "cwd", None))
        if gated is not None:
            return gated

        # 与同步 execute 一致：先做 heredoc 还原，再做工作区路径改写。
        command = maybe_unescape_heredoc_newlines(command)

        cmd_for_container = self._rewrite_host_workspace_paths_for_container(command)

        timeout_ms = (timeout or 120) * 1000
        exec_command = cmd_for_container
        if (
            bg_execute_enabled()
            and is_probably_long_running_server(command)
            and not command_already_backgrounded(command)
        ):
            log_rel = f".smartclaw_bg/bg_{uuid.uuid4().hex[:12]}.log"
            exec_command = wrap_command_linux_container(
                cmd_for_container,
                workspace_posix=self.container_workspace,
                log_relpath=log_rel,
            )

        try:
            result = await self._do_docker_execute(exec_command, timeout_ms)
            if is_deepagents_verbose():
                info(f"[Docker-Backend.aexecute] Docker 执行完成: exit={result.exit_code}")
            out = result.stdout + ("\n" + result.stderr if result.stderr else "")
            if exec_command != cmd_for_container:
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
                            _pr = await self._do_docker_execute(_probe_cmd, _probe_ms)
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
                        workspace_posix=self.container_workspace,
                    )
                    if _scm:
                        _sm_ms = min(
                            900_000,
                            int(decl.after_smoke_timeout_sec * 1000) + 10_000,
                        )
                        try:
                            _sr = await self._do_docker_execute(_scm, _sm_ms)
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
            error(f"[Docker-Backend.aexecute] Docker 执行失败: {e}")
            return ExecuteResponse(
                output="",
                stderr=f"[Docker-Backend] Docker 执行失败: {e}",
                exit_code=1,
                truncated=False,
            )

    def get_container_logs(self, lines: int = 100) -> str:
        """获取容器日志"""
        if not self.container_id:
            return "容器不存在"

        result = subprocess.run(
            ["docker", "logs", "--tail", str(lines), self.container_id],
            capture_output=True,
            text=True,
            **SUBPROCESS_TEXT_KWARGS,
        )

        return result.stdout + result.stderr


__all__ = ["DockerDeepAgentsBackend"]
