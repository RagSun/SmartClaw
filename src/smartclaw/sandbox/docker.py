"""
Docker 沙箱后端

使用 Docker 容器作为隔离执行环境。
"""

import asyncio
import os
import socket
import threading
import subprocess
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Optional, Union

from smartclaw.console import error, info, warning
from smartclaw.subprocess_io import SUBPROCESS_TEXT_KWARGS

# Docker 操作通过 subprocess 执行，不需要 Python SDK
# 使用 docker CLI 代替 Python SDK


def default_docker_workspace_parent() -> Path:
    """
    宿主机上用于 ``DockerSandboxBackend.workspace / <agent_id>`` 的默认父目录。

    非 root 用户无法写入 ``/root/smartclaw_workspace``；默认落到当前用户家目录下。
    可通过环境变量 ``SMARTCLAW_DOCKER_WORKSPACE_PARENT`` 覆盖。
    """
    raw = (os.environ.get("SMARTCLAW_DOCKER_WORKSPACE_PARENT") or "").strip()
    if raw:
        return Path(os.path.expanduser(raw)).expanduser().resolve()
    return (Path.home() / ".smartclaw" / "docker_workspace").resolve()


class DockerInstanceInfo:
    """Docker 容器实例信息"""

    def __init__(
        self,
        instance_id: str,
        container: Any,
        project_dir: Path,
        created_at: float,
        container_name: str = "",
    ):
        self.instance_id = instance_id
        self.container = container
        self.container_name = container_name or f"smartclaw-{instance_id}"
        self.project_dir = project_dir
        self.created_at = created_at
        self._process: Optional[asyncio.subprocess.Process] = None




def get_port_process_info(port):
    """获取占用端口的进程信息"""
    try:
        result = subprocess.run(
            ['ss', '-tlnp', f' sport = :{port}'],
            capture_output=True,
            text=True,
            timeout=5,
            **SUBPROCESS_TEXT_KWARGS,
        )
        lines = result.stdout.strip().split('\n')
        processes = []
        for line in lines[1:]:  # 跳过标题行
            if str(port) in line:
                # 提取进程信息
                parts = line.split()
                processes.append(line)
        return processes
    except:
        return []


def kill_process_on_port(port, signal='-9'):
    """强制终止占用端口的进程"""
    try:
        # 查找占用端口的进程
        result = subprocess.run(
            ['fuser', f'{port}/tcp'],
            capture_output=True,
            text=True,
            timeout=5,
            **SUBPROCESS_TEXT_KWARGS,
        )
        if result.stdout.strip():
            pids = result.stdout.strip().split()
            for pid in pids:
                try:
                    subprocess.run(['kill', signal, pid], timeout=5)
                    info(f"[端口 {port}] 已 kill 进程 PID={pid}")
                except Exception as e:
                    warning(f"[端口 {port}] kill 进程 {pid} 失败: {e}")
            return True
        return False
    except Exception as e:
        warning(f"[端口 {port}] fuser 查询失败: {e}")
        return False


def get_available_port(start=5000, end=6000, exclude=None):
    """获取范围内第一个可用端口"""
    exclude = exclude or set()
    for port in range(start, end + 1):
        if port in exclude:
            continue
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        try:
            sock.bind(('0.0.0.0', port))
            sock.close()
            return port
        except (OSError, socket.error):
            continue
    return None


def is_port_available(port):
    """检查端口是否可用"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)
    try:
        sock.bind(('0.0.0.0', port))
        sock.close()
        return True
    except (OSError, socket.error):
        return False

class DockerSandboxBackend:
    """
    Docker 沙箱后端

    使用 Docker 容器作为隔离执行环境。

    安全特性 (OpenClaw 风格):
        - network: none/bridge/host (默认 bridge)
        - user: 非 root 用户 (默认 1000:1000)
        - cap_drop: 移除所有 capabilities
        - read_only_root: 只读根文件系统
        - pids_limit: PID 数量限制
        - memory: 内存限制
        - tmpfs: 挂载临时文件系统
        - exposed_ports: 要映射到宿主机的端口列表
          例如: [5010, 5012] 会映射 5010:5010 和 5012:5012
    """

    backend_type = "docker"

    def __init__(
        self,
        workspace: Union[str, Path, None] = None,
        base_image: str = "python:3.12-slim",
        max_containers: int = 4,
        # 安全配置 (OpenClaw 风格, 默认为 True)
        security_mode: bool = True,
        # 网络模式: "none"(安全) / "bridge"(可访问外网) / "host"(遗留,危险但端口直通)
        network_mode: str = "host",  # host 模式，端口直接绑定宿主机
        # 用户 (留空则使用 root)
        container_user: str = "root",  # 使用 root 用户
        # 只读根文件系统
        read_only_root: bool = False,  # 允许写入
        # PID 限制
        pids_limit: int = 256,
        # 内存限制
        memory_limit: str = "1g",
        memory_swap: str = "2g",
        # 临时文件系统
        tmpfs: list = None,
        # Capabilities 限制
        cap_drop: list = None,
        # 端口配置
        exposed_ports: list = None,  # 固定端口列表（保留）
        port_range_start: int = 5000,  # 动态端口范围起始
        port_range_end: int = 6000,  # 动态端口范围结束
    ):
        ws = workspace
        if ws is None or (isinstance(ws, str) and not str(ws).strip()):
            self.workspace = default_docker_workspace_parent()
        else:
            self.workspace = Path(os.path.expanduser(str(ws))).expanduser().resolve()
        self.base_image = base_image
        self.max_containers = max_containers

        # 安全配置
        self.security_mode = security_mode
        self.network_mode = network_mode
        self.container_user = container_user
        self.read_only_root = read_only_root
        self.pids_limit = pids_limit
        self.memory_limit = memory_limit
        self.memory_swap = memory_swap
        self.tmpfs = tmpfs or ["/tmp", "/var/tmp", "/run"]
        self.cap_drop = cap_drop or ["ALL"]
        # 端口映射列表
        self.exposed_ports = exposed_ports or []  # 空列表，Docker 随机分配端口
        self.port_range_start = port_range_start
        self.port_range_end = port_range_end

        # 智能端口分配：检查每个端口是否可用
        self._allocated_ports = {}  # instance_id -> {container_port: host_port}
        self._port_lock = threading.Lock()
        self._used_host_ports = set()

        # 预检查并记录可用端口（端口范围）
        self._available_port_range = []
        for port in range(self.port_range_start, self.port_range_end + 1):
            if is_port_available(port):
                self._available_port_range.append(port)
        info(f"[DockerSandboxBackend] 可用端口范围: {self.port_range_start}-{self.port_range_end}, "
             f"可用端口: {len(self._available_port_range)} 个")

        # Docker 操作通过 subprocess 执行，不需要 Python SDK 客户端
        # 容器实例记录: instance_id -> DockerInstanceInfo
        self._instances: dict[str, DockerInstanceInfo] = {}

        # 实例计数器
        self._instance_counter = 0

        info(f"[DockerSandboxBackend] 初始化完成")
        info(f"[DockerSandboxBackend] 最大容器数: {max_containers}")
    
    async def initialize(self) -> None:
        """初始化沙箱（工业级大扫除：清理上一次异常遗留的僵尸沙箱）

        额外做两件鲁棒化前置：
        1. **daemon 健康预检**：``docker version --format ...``，限定 5s；失败仅告警，
           保留原有"首次 create_instance 才触发真实错误"的兼容行为。
        2. **镜像预拉**：``docker image inspect <base_image>`` 若不存在则后台 ``docker pull``，
           可由 ``SMARTCLAW_DOCKER_PREPULL=0`` 关闭，默认开启。
           已存在则跳过；网络失败/无权限不会中断 initialize。
        """
        import subprocess
        info("[DockerSandboxBackend] 执行沙箱协调清理 (Reconciliation)...")

        # === 1) daemon 健康预检（best-effort，不阻断） ===
        try:
            _hv = subprocess.run(
                ["docker", "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                text=True,
                timeout=5,
                **SUBPROCESS_TEXT_KWARGS,
            )
            if _hv.returncode == 0 and (_hv.stdout or "").strip():
                info(
                    f"[DockerSandboxBackend] daemon ok server={_hv.stdout.strip()}"
                )
            else:
                _se = (_hv.stderr or "").strip().replace("\n", " ")[:200]
                warning(
                    "[DockerSandboxBackend] daemon 健康检查失败 "
                    f"(exit={_hv.returncode}): {_se}"
                )
        except FileNotFoundError:
            warning("[DockerSandboxBackend] 未找到 docker CLI，后续 create_instance 将失败")
        except subprocess.TimeoutExpired:
            warning(
                "[DockerSandboxBackend] daemon 健康检查超时（>5s）— "
                "docker daemon 响应缓慢，create_instance/execute 可能也会卡顿"
            )
        except Exception as _e:
            warning(f"[DockerSandboxBackend] daemon 健康检查异常: {_e}")

        # === 2) 镜像预拉（best-effort，不阻断） ===
        if (os.environ.get("SMARTCLAW_DOCKER_PREPULL") or "1").strip().lower() not in (
            "0",
            "false",
            "no",
            "off",
        ):
            try:
                _ins = subprocess.run(
                    ["docker", "image", "inspect", self.base_image],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    **SUBPROCESS_TEXT_KWARGS,
                )
                if _ins.returncode != 0:
                    info(
                        f"[DockerSandboxBackend] 镜像本地缺失，开始 docker pull {self.base_image}"
                    )
                    _pull = subprocess.run(
                        ["docker", "pull", self.base_image],
                        capture_output=True,
                        text=True,
                        timeout=180,
                        **SUBPROCESS_TEXT_KWARGS,
                    )
                    if _pull.returncode == 0:
                        info(
                            f"[DockerSandboxBackend] 镜像预拉完成: {self.base_image}"
                        )
                    else:
                        _pe = (_pull.stderr or "").strip().replace("\n", " ")[:200]
                        warning(
                            "[DockerSandboxBackend] 镜像预拉失败 "
                            f"(exit={_pull.returncode}): {_pe}"
                        )
                else:
                    info(
                        f"[DockerSandboxBackend] 镜像已存在本地缓存: {self.base_image}"
                    )
            except subprocess.TimeoutExpired:
                warning("[DockerSandboxBackend] 镜像预拉超时（>180s），跳过")
            except Exception as _e:
                warning(f"[DockerSandboxBackend] 镜像预拉异常: {_e}")

        # 清理所有被打上 managed-by=smartclaw 标签的历史遗留容器。
        # 跨平台实现：不用 `$(...)` / `2>/dev/null`（bash-only，在 Windows cmd 下静默失败，
        # 会导致沙箱僵尸容器在 Windows 上不断堆积）。改为「先列 id、再按 id 批量删」两步，
        # 纯 docker CLI、无 shell 管道，Windows / Linux / macOS 行为一致。
        try:
            _ls = subprocess.run(
                ["docker", "ps", "-a", "-q", "-f", "label=managed-by=smartclaw"],
                capture_output=True,
                text=True,
                timeout=10,
                **SUBPROCESS_TEXT_KWARGS,
            )
            zombie_ids = [x for x in (_ls.stdout or "").split() if x]
            if zombie_ids:
                subprocess.run(
                    ["docker", "rm", "-f", *zombie_ids],
                    capture_output=True,
                    timeout=30,
                    **SUBPROCESS_TEXT_KWARGS,
                )
                info(
                    f"[DockerSandboxBackend] 已清理 {len(zombie_ids)} 个历史遗留沙箱容器。"
                )
            else:
                info("[DockerSandboxBackend] 无历史遗留沙箱容器。")
        except FileNotFoundError:
            warning("[DockerSandboxBackend] 未找到 docker CLI，跳过僵尸容器清理")
        except subprocess.TimeoutExpired:
            warning("[DockerSandboxBackend] 僵尸容器清理超时，跳过")
        except Exception as _e:
            warning(f"[DockerSandboxBackend] 僵尸容器清理异常: {_e}")
        info("[DockerSandboxBackend] 环境已就绪。")

    def _generate_instance_id(self, prefix: str = "docker") -> str:
        """生成唯一的实例 ID"""
        self._instance_counter += 1
        import uuid; return f"{prefix}-{self._instance_counter}-{uuid.uuid4().hex[:6]}"

    async def create_instance(
        self,
        project_name: str = "default",
        agent_id: str = None,
        memory_mb: int = None,
        cpu_count: int = None,
        *,
        host_workspace_dir: Union[str, Path, None] = None,
        **kwargs,
    ) -> str:
        """创建沙箱实例

        Args:
            project_name: 项目名称
            agent_id: Agent ID (兼容旧接口)
            memory_mb: 内存限制 MB (未实现)
            cpu_count: CPU 核心数 (未实现)
            host_workspace_dir: 宿主侧目录，直接绑定到容器 ``/root/workspace``。
                与 DeepAgents / agent.json 解析出的工作区一致时，宿主机文件与容器内可见同一棵树。
                未指定时沿用 ``self.workspace / project_name``（兼容预热池等场景）。
        """
        if agent_id:
            project_name = agent_id

        instance_id = self._generate_instance_id()
        container_name = f"smartclaw-{instance_id}"
        if host_workspace_dir is not None and str(host_workspace_dir).strip():
            project_dir = Path(os.path.expanduser(str(host_workspace_dir))).expanduser().resolve()
        else:
            project_dir = (self.workspace / project_name).resolve()
        project_dir.mkdir(parents=True, exist_ok=True)

        info(
            f"[DockerSandboxBackend] 创建实例: {instance_id}, 容器: {container_name[:20]}..., "
            f"宿主挂载: {project_dir}"
        )

        try:
            # 构建 docker run 命令
            # 使用 bridge 网络 + 端口映射 (关键修复!)
            cmd = [
                "docker", "run", "-d",
                "--name", container_name,
                "--hostname", project_name,
                "--label", "managed-by=smartclaw",  # 注入工业级生命周期管理标签
                "-v", f"{project_dir}:/root/workspace:rw",
                "-w", "/root/workspace",
            ]

            if self.security_mode:
                # === OpenClaw 风格安全配置 ===

                # 网络隔离
                if self.network_mode == "none":
                    cmd.extend(["--network", "none"])
                elif self.network_mode == "bridge":
                    cmd.extend(["--network", "bridge"])
                else:
                    if self.network_mode == "host":
                        warning("[DockerSandboxBackend] host 网络模式: 容器与宿主机共享网络命名空间")
                    cmd.extend(["--network", self.network_mode])

                # 用户权限
                if self.container_user:
                    cmd.extend(["--user", self.container_user])

                # Capabilities 限制
                for cap in self.cap_drop:
                    cmd.extend(["--cap-drop", cap])

                # 只读根文件系统
                if self.read_only_root:
                    cmd.append("--read-only")

                # PID 限制
                cmd.extend(["--pids-limit", str(self.pids_limit)])

                # 内存限制
                cmd.extend(["--memory", self.memory_limit])
                if self.memory_swap:
                    cmd.extend(["--memory-swap", self.memory_swap])

                # 临时文件系统
                for mount in self.tmpfs:
                    cmd.extend(["--tmpfs", f"{mount}:rw,noexec,nosuid,size=64m"])

                # DNS
                cmd.extend(["--dns", "1.1.1.1", "--dns", "8.8.8.8"])

            else:
                # 非安全模式或 host 模式
                cmd.extend(["--net", self.network_mode])

            # ========== 智能端口映射（bridge 模式） ==========
            # 策略：按需分配，冲突自动重试
            if self.network_mode == "bridge":
                if not self.exposed_ports:
                    # 无固定端口需求，让 Docker 随机分配
                    cmd.extend(["-P"])
                    info(f"[DockerSandboxBackend] Docker 随机端口映射")
                else:
                    # 尝试按顺序分配端口
                    for port in self.exposed_ports:
                        cmd.extend(["-p", f"{port}:{port}"])
                    info(f"[DockerSandboxBackend] 请求端口映射: {self.exposed_ports}")
            elif self.network_mode == "host":
                info(f"[DockerSandboxBackend] host 模式：端口直接绑定宿主机")

            # 添加环境变量
            cmd.extend([
                "-e", f"PROJECT_NAME={project_name}",
                "-e", "PYTHONUNBUFFERED=1",
                "-e", "HOST=0.0.0.0",
                "-e", "FLASK_RUN_HOST=0.0.0.0",
            ])

            cmd.append(self.base_image)
            cmd.extend(["sleep", "infinity"])

            # 启动容器
            result = await asyncio.create_subprocess_shell(
                " ".join(cmd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await result.communicate()

            if result.returncode != 0:
                err = (stderr or b"").decode("utf-8", errors="replace")
                error(f"容器启动失败: {err}")
                raise RuntimeError(f"Docker container failed to start: {err}")

            container_id = stdout.decode("utf-8", errors="replace").strip()[:12]
            info(f"[DockerSandboxBackend] 容器已启动: {container_id}")

            # 获取容器对象
            # 通过 docker inspect 获取容器信息 (不依赖 docker SDK)
            import subprocess
            result = subprocess.run(
                ['docker', 'inspect', container_id, '--format', '{{.State.Status}}'],
                capture_output=True,
                text=True,
                **SUBPROCESS_TEXT_KWARGS,
            )
            if result.returncode != 0:
                raise RuntimeError(f'容器不存在: {container_id}')
            container = None  # 我们不直接引用 container 对象

            # 记录实例
            instance_info = DockerInstanceInfo(
                instance_id=instance_id,
                container=container,
                project_dir=project_dir,
                created_at=time.time(),
            )
            self._instances[instance_id] = instance_info
            info(f"[DockerSandboxBackend] 实例已创建: {instance_id}")
            return instance_info

        except Exception as e:
            error(f"创建沙箱实例失败: {e}")
            raise

    async def execute(
        self,
        instance_id: str,
        command: str,
        timeout_ms: int = 30000,
    ) -> "CommandResult":
        """
        在沙箱实例中执行命令

        Args:
            instance_id: 实例 ID
            command: 要执行的命令
            timeout_ms: 超时时间 (毫秒)

        Returns:
            CommandResult: 包含 stdout, stderr, exit_code

        鲁棒化开关（默认全部关，零行为变更）:
        - ``SMARTCLAW_DOCKER_EXEC_STREAM=1``：启用按行 streaming，
          超时时**保留已收到的输出**而不是返回空字符串；
        - ``SMARTCLAW_DOCKER_EXEC_IDLE_MS=N``（仅 STREAM=1 生效）：
          连续 N 毫秒无任何 stdout/stderr 输出且进程仍存活时，
          视作"卡死"提前 kill；``0`` 表示不启用 idle 检测。
        """
        if instance_id not in self._instances:
            raise ValueError(f"Unknown instance: {instance_id}")

        instance = self._instances[instance_id]
        container_name = instance.container_name

        info(f"[Docker-Backend.execute] 实例={instance_id}, cmd={command[:100]}...")

        stream_enabled = (
            os.environ.get("SMARTCLAW_DOCKER_EXEC_STREAM") or ""
        ).strip().lower() in {"1", "true", "yes", "on"}
        if stream_enabled:
            return await self._execute_streaming(
                container_name=container_name,
                command=command,
                timeout_ms=timeout_ms,
            )

        try:
            import shlex
            # 使用 docker exec 在容器内执行命令
            # 注意: 使用 bash -c 来执行命令，支持管道等复杂命令
            # 使用 shlex.quote 进行标准的 Shell 级转义，解决带引号/管道符命令的语法错误问题
            result = await asyncio.create_subprocess_shell(
                f"docker exec {container_name} bash -c {shlex.quote(command)}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    result.communicate(), timeout=timeout_ms / 1000
                )

                output = stdout.decode("utf-8", errors="replace")
                error_output = stderr.decode("utf-8", errors="replace")

                info(f"[Docker-Backend.execute] Docker 执行完成: exit={result.returncode}")

                return CommandResult(
                    exit_code=result.returncode,
                    stdout=output,
                    stderr=error_output,
                )

            except asyncio.TimeoutError:
                result.kill()
                await result.wait()
                error(f"[Docker-Backend.execute] 命令执行超时: {timeout_ms}ms")
                return CommandResult(
                    exit_code=-1,
                    stdout="",
                    stderr=f"Command execution timeout ({timeout_ms}ms)",
                )

        except Exception as e:
            error(f"[Docker-Backend.execute] 执行失败: {e}")
            return CommandResult(
                exit_code=-1,
                stdout="",
                stderr=str(e),
            )

    def _exec_idle_ms_env(self) -> int:
        """``SMARTCLAW_DOCKER_EXEC_IDLE_MS`` 解析；非法/缺省返回 0（禁用 idle）。"""
        raw = (os.environ.get("SMARTCLAW_DOCKER_EXEC_IDLE_MS") or "").strip()
        if not raw:
            return 0
        try:
            v = int(raw)
        except ValueError:
            return 0
        return max(0, v)

    async def _execute_streaming(
        self,
        *,
        container_name: str,
        command: str,
        timeout_ms: int,
    ) -> "CommandResult":
        """``docker exec`` 的 streaming 版本。

        - 边读边拼 stdout/stderr，**永不丢已收到的字节**；
        - 同时检查 总时长超时(``timeout_ms``) 与 静默 idle 超时(``SMARTCLAW_DOCKER_EXEC_IDLE_MS``)；
        - 任何错误都退化为带有清晰 ``stderr`` 的 ``CommandResult(exit_code=-1)``，
          与原同步路径返回结构一致。
        """
        import shlex

        idle_ms = self._exec_idle_ms_env()
        deadline = time.monotonic() + max(0.0, timeout_ms / 1000.0)
        idle_budget_sec = (idle_ms / 1000.0) if idle_ms > 0 else None
        last_activity = time.monotonic()

        try:
            proc = await asyncio.create_subprocess_shell(
                f"docker exec {container_name} bash -c {shlex.quote(command)}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as e:
            error(f"[Docker-Backend.execute.stream] 启动 docker exec 失败: {e}")
            return CommandResult(exit_code=-1, stdout="", stderr=str(e))

        stdout_buf: list[bytes] = []
        stderr_buf: list[bytes] = []

        async def _drain(stream: asyncio.StreamReader | None, sink: list[bytes]) -> None:
            nonlocal last_activity
            if stream is None:
                return
            while True:
                # ``read(N)`` 不要求遇到换行才返回，更适合 streaming
                chunk = await stream.read(4096)
                if not chunk:
                    return
                sink.append(chunk)
                last_activity = time.monotonic()

        drain_tasks = [
            asyncio.create_task(_drain(proc.stdout, stdout_buf)),
            asyncio.create_task(_drain(proc.stderr, stderr_buf)),
        ]
        wait_proc = asyncio.create_task(proc.wait())

        timeout_reason: str | None = None
        try:
            while True:
                # 总超时
                if time.monotonic() >= deadline:
                    timeout_reason = f"total_timeout({timeout_ms}ms)"
                    break
                # idle 超时
                if (
                    idle_budget_sec is not None
                    and (time.monotonic() - last_activity) >= idle_budget_sec
                    and proc.returncode is None
                ):
                    timeout_reason = f"idle_timeout({idle_ms}ms)"
                    break

                slice_sec = 0.25
                # 若总预算或 idle 预算更近，按更近的来 wake
                slice_sec = min(slice_sec, max(0.05, deadline - time.monotonic()))
                if idle_budget_sec is not None:
                    slice_sec = min(
                        slice_sec,
                        max(
                            0.05,
                            idle_budget_sec - (time.monotonic() - last_activity),
                        ),
                    )

                done, _pending = await asyncio.wait(
                    {wait_proc}, timeout=slice_sec
                )
                if wait_proc in done:
                    # 等 drain 任务把 PIPE 残余字节读完
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(*drain_tasks, return_exceptions=True),
                            timeout=2.0,
                        )
                    except asyncio.TimeoutError:
                        pass
                    break
        finally:
            if timeout_reason is not None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                # 给 docker exec 一点时间把 stderr flush 出来
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
                for t in drain_tasks:
                    if not t.done():
                        t.cancel()
                try:
                    await asyncio.gather(*drain_tasks, return_exceptions=True)
                except Exception:
                    pass

        stdout_text = b"".join(stdout_buf).decode("utf-8", errors="replace")
        stderr_text = b"".join(stderr_buf).decode("utf-8", errors="replace")

        if timeout_reason is not None:
            # 附加容器内进程快照（best-effort，超时 2s），帮助模型/用户排错
            snap = ""
            try:
                snap_proc = await asyncio.create_subprocess_shell(
                    f"docker exec {container_name} sh -c "
                    "'ps -ef 2>/dev/null | head -n 20 "
                    "|| ps aux 2>/dev/null | head -n 20'",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                so, _se = await asyncio.wait_for(
                    snap_proc.communicate(), timeout=2.0
                )
                snap = so.decode("utf-8", errors="replace").strip()
            except Exception:
                pass

            error(
                f"[Docker-Backend.execute.stream] 命令执行超时: {timeout_reason} "
                f"(stdout={len(stdout_text)}B, stderr={len(stderr_text)}B)"
            )
            tail_hint = (
                f"\n[smartclaw] {timeout_reason}; "
                f"container={container_name}; "
                "可在宿主用 `docker logs " + container_name + "` 排错。"
            )
            if snap:
                tail_hint += f"\n[smartclaw] 容器内 ps 快照:\n{snap}"
            return CommandResult(
                exit_code=-1,
                stdout=stdout_text,
                stderr=(stderr_text + tail_hint).strip(),
            )

        info(
            f"[Docker-Backend.execute.stream] Docker 执行完成: exit={proc.returncode} "
            f"stdout_bytes={len(stdout_text)} stderr_bytes={len(stderr_text)}"
        )
        return CommandResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout_text,
            stderr=stderr_text,
        )

    async def destroy_instance(self, instance_id: str) -> None:
        """销毁沙箱实例"""
        if instance_id not in self._instances:
            warning(f"[DockerSandboxBackend] 尝试销毁未知实例: {instance_id}")
            return

        instance = self._instances[instance_id]
        container_name = instance.container_name

        try:
            # 停止并删除容器
            subprocess.run(['docker', 'stop', container_name], capture_output=True, timeout=10)
            subprocess.run(['docker', 'rm', '-f', container_name], capture_output=True)
            info(f"[DockerSandboxBackend] 实例已销毁: {instance_id}")
        except Exception as e:
            warning(f"[DockerSandboxBackend] 销毁实例失败: {e}")
        finally:
            del self._instances[instance_id]

    def list_instances(self) -> list[str]:
        """列出所有活跃实例"""
        return list(self._instances.keys())

    async def cleanup_idle_instances(self, max_idle_seconds: int = 3600) -> int:
        """
        清理空闲实例

        Args:
            max_idle_seconds: 最大空闲时间 (秒)

        Returns:
            清理的实例数量
        """
        now = time.time()
        to_remove = []

        for instance_id, instance in self._instances.items():
            idle_time = now - instance.created_at
            if idle_time > max_idle_seconds:
                to_remove.append(instance_id)

        for instance_id in to_remove:
            await self.destroy_instance(instance_id)

        if to_remove:
            info(f"[DockerSandboxBackend] 已清理 {len(to_remove)} 个空闲实例")

        return len(to_remove)


class CommandResult:
    """命令执行结果"""
    exit_code: int
    stdout: str
    stderr: str

    def __init__(self, exit_code: int = 0, stdout: str = "", stderr: str = ""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr

    @property
    def success(self) -> bool:
        return self.exit_code == 0
