"""
将 DeepAgents 的 LocalShellBackend 约束在单一 Agent 工作区根目录下。

- virtual_mode=True：FilesystemBackend 的 read/write/list 等遵守 root，禁止 .. / ~ 与根外绝对路径。
- execute：仍可能被 mkdir C:\\... 逃逸，故对宿主 shell 命令做额外扫描并拒绝明显越界路径。
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from deepagents.backends import LocalShellBackend
from deepagents.backends.protocol import EditResult, ExecuteResponse, WriteResult

from smartclaw.agent.deepagents_shell_gate import (
    gate_deepagents_execute,
    gate_deepagents_file_write,
)
from smartclaw.agent.bg_execute import (
    bg_execute_enabled,
    command_already_backgrounded,
    is_probably_long_running_server,
    launch_local_shell_detached,
    unwrap_windows_start_background,
)
from smartclaw.agent.tools.loop_detector import TOOL_DEEPAGENTS_SHELL, get_loop_detector
from smartclaw.debug_session_log import debug_ndjson
from smartclaw.subprocess_io import SUBPROCESS_TEXT_KWARGS

# Windows: C:\foo、D:/bar（截到空白或 shell 元字符前）
_WIN_ABS_PATH = re.compile(
    r'(?i)(?<![A-Za-z0-9_])([A-Za-z]:(?:\\\\|\\|/)(?:[^\s"\'`|&;<>]+))',
)
# UNC: \\server\share\...
_UNC_PATH = re.compile(r'(?i)(?<![A-Za-z0-9_])(\\\\[^\s"\'`|&;<>]+\\[^\s"\'`|&;<>]+)')
# 在工作区外先 cd 到上级再写文件
_CD_PARENT = re.compile(r'(?i)(?:^|[;&|]|\|\||&&)\s*cd\s+\.\.')
_FILE_URL_WORKSPACE = re.compile(r"(?i)^file:///workspace(?:/|$)")


def normalize_workspace_tool_path(file_path: str) -> str:
    """
    将模型常见的容器/文档伪路径归一为 DeepAgents virtual root 下的路径。

    DeepAgents virtual_mode=True 时，`/foo` 会映射为 `{root}/foo`。如果模型使用
    `/workspace/docs/a.md`，未归一化会落成 `{root}/workspace/docs/a.md`。这里对齐
    OpenClaw 的做法，将 `/workspace/...` 视为当前工作区根。
    """
    raw = (file_path or "").strip()
    if not raw:
        return file_path

    prefix = ""
    candidate = raw
    if candidate.startswith("@"):
        prefix = "@"
        candidate = candidate[1:]

    candidate = candidate.replace("\\", "/")
    if _FILE_URL_WORKSPACE.match(candidate):
        candidate = "/" + candidate[len("file:///workspace") :].lstrip("/")
    elif candidate == "/workspace":
        candidate = "/"
    elif candidate.startswith("/workspace/"):
        candidate = "/" + candidate[len("/workspace/") :]
    elif candidate == "workspace":
        candidate = "."
    elif candidate.startswith("workspace/"):
        candidate = candidate[len("workspace/") :]

    if candidate.startswith("//"):
        # 不碰 UNC / 网络路径形态；交给后续安全扫描。
        return raw
    return prefix + candidate


def _is_under_root(candidate: Path, root: Path) -> bool:
    try:
        c = candidate.resolve()
        r = root.resolve()
        c.relative_to(r)
        return True
    except (ValueError, OSError):
        return False


def _scan_command_for_escape(command: str, root: Path) -> str | None:
    """
    若检测到命令试图操作 root 之外的绝对路径，返回错误说明；否则 None。
    （启发式：无法覆盖全部 shell 变体；生产环境请使用 Docker/Firecracker 沙箱。）
    """
    if not command or not command.strip():
        return None
    if _CD_PARENT.search(command):
        return (
            "拒绝执行：命令含有在工作区根目录之外切换目录（例如 cd ..）。"
            f" 请仅在当前工作区内使用相对路径（工作区根: {root}）。"
        )

    root_resolved = root.resolve()

    for m in _WIN_ABS_PATH.finditer(command):
        raw = m.group(1)
        if not raw:
            continue
        trimmed = raw.split("&")[0].split("|")[0].strip().rstrip(')"\'')
        p = Path(trimmed)
        try:
            if p.is_absolute() and not _is_under_root(p, root_resolved):
                return (
                    "拒绝执行：命令包含工作区外的绝对路径 "
                    f"{raw!r}。所有文件与目录须写在 Agent 工作区内: {root_resolved}"
                )
        except OSError:
            return f"拒绝执行：无法解析路径 {raw!r}。"

    if os.name == "nt":
        for m in _UNC_PATH.finditer(command):
            raw = m.group(0)
            p = Path(raw)
            try:
                if not _is_under_root(p, root_resolved):
                    return (
                        "拒绝执行：命令包含网络路径或工作区外 UNC "
                        f"{raw!r}。请仅在工作区内创建内容: {root_resolved}"
                    )
            except OSError:
                return f"拒绝执行：无法解析 UNC 路径 {raw!r}。"

    # 类 Unix 主机上的绝对路径（避免在 Windows 上误伤含 "/options" 的命令）
    if os.name != "nt":
        for m in re.finditer(r'(?:^|\s)(/[^\s"\'`|&;<>]+)', command):
            part = m.group(1)
            if part in ("/", "//"):
                continue
            try:
                p = Path(part)
                if p.is_absolute() and p.parts and not _is_under_root(p, root_resolved):
                    return (
                        "拒绝执行：命令包含工作区外的绝对路径 "
                        f"{part!r}。工作区根: {root_resolved}"
                    )
            except OSError:
                continue

    return None


class WorkspaceBoundLocalShellBackend(LocalShellBackend):
    """
    与 LocalShellBackend 相同，但：
    - 强制 virtual_mode=True（文件操作不能越出 root_dir）。
    - execute 前扫描越界绝对路径与 cd .. 逃逸。
    """

    def __init__(
        self,
        root_dir: str | Path,
        *,
        env: dict[str, str] | None = None,
        inherit_env: bool = False,
        timeout: int = 120,
        max_output_bytes: int = 100_000,
    ) -> None:
        super().__init__(
            root_dir=root_dir,
            virtual_mode=True,
            timeout=timeout,
            max_output_bytes=max_output_bytes,
            env=env,
            inherit_env=inherit_env,
        )
        self._write_records: list[dict[str, str]] = []

    @property
    def write_records(self) -> list[dict[str, str]]:
        return list(self._write_records)

    def _record_write(self, requested_path: str, normalized_path: str) -> None:
        try:
            resolved = self._resolve_path(normalized_path)
        except Exception:
            resolved = self.cwd / normalized_path.lstrip("/\\")
        self._write_records.append(
            {
                "requested_path": requested_path,
                "normalized_path": normalized_path,
                "host_path": str(resolved),
            }
        )

    def _resolve_path(self, key: str) -> Path:
        return super()._resolve_path(normalize_workspace_tool_path(key))

    def write(self, file_path: str, content: str) -> Any:
        denied = gate_deepagents_file_write("write_file")
        if denied:
            return WriteResult(error=f"DeepAgents write_file 被权限门禁拒绝：{denied}")
        normalized = normalize_workspace_tool_path(file_path)
        result = super().write(normalized, content)
        if not getattr(result, "error", None):
            self._record_write(file_path, normalized)
        return result

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> Any:
        denied = gate_deepagents_file_write("edit_file")
        if denied:
            return EditResult(error=f"DeepAgents edit_file 被权限门禁拒绝：{denied}")
        normalized = normalize_workspace_tool_path(file_path)
        result = super().edit(normalized, old_string, new_string, replace_all)
        if not getattr(result, "error", None):
            self._record_write(file_path, normalized)
        return result

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        det = get_loop_detector()

        def _finish(resp: ExecuteResponse) -> ExecuteResponse:
            if det and isinstance(command, str):
                det.record(
                    command,
                    TOOL_DEEPAGENTS_SHELL,
                    resp.exit_code == 0,
                    (resp.output or "")[:500],
                )
            if isinstance(command, str) and (
                "smartclaw_bg" in command
                or (".log" in command and "type" in command.lower())
            ):
                # region agent log
                out = resp.output or ""
                debug_ndjson(
                    "H2",
                    "workspace_bound_backend.py:execute_finish",
                    "shell finish (bg log / type log)",
                    {
                        "exit_code": resp.exit_code,
                        "out_len": len(out),
                        "cmd_preview": command[:160].replace("\n", " "),
                        "out_head": out[:200].replace("\n", "\\n"),
                    },
                )
                # endregion
            return resp

        block = _scan_command_for_escape(command, self.cwd)
        if block:
            # region agent log
            debug_ndjson(
                "H-B",
                "workspace_bound_backend.py:execute_blocked_escape",
                "command blocked by workspace path scan",
                {
                    "cwd": str(self.cwd),
                    "block_preview": block[:400],
                    "cmd_preview": command[:220].replace("\n", " "),
                },
            )
            # endregion
            return _finish(ExecuteResponse(output=block, exit_code=1, truncated=False))

        if det:
            lr = det.check_proposed(TOOL_DEEPAGENTS_SHELL, command)
            if lr.is_loop:
                return ExecuteResponse(
                    output=lr.suggested_action,
                    exit_code=1,
                    truncated=False,
                )

        gated = gate_deepagents_execute(command, workspace_root=self.cwd)
        if gated is not None:
            # region agent log
            go = gated.output or ""
            debug_ndjson(
                "H-B",
                "workspace_bound_backend.py:execute_blocked_gate",
                "command gated by gate_deepagents_execute",
                {
                    "cwd": str(self.cwd),
                    "exit_code": gated.exit_code,
                    "gate_out_head": go[:400],
                    "cmd_preview": command[:220].replace("\n", " "),
                },
            )
            # endregion
            return _finish(gated)

        # 不重载父类则走 deepagents LocalShellBackend：text=True 且无 encoding 时在 Windows
        # 上按 GBK 读管道，遇 UTF-8 输出会在 _readerthread 崩溃，主流程无法捕获。
        if not command or not isinstance(command, str):
            return _finish(
                ExecuteResponse(
                    output="Error: Command must be a non-empty string.",
                    exit_code=1,
                    truncated=False,
                )
            )

        effective_timeout = timeout if timeout is not None else self._default_timeout
        if effective_timeout <= 0:
            msg = f"timeout must be positive, got {effective_timeout}"
            raise ValueError(msg)

        bg_command = unwrap_windows_start_background(command)
        # region agent log
        _will_detach = bool(
            bg_execute_enabled()
            and is_probably_long_running_server(bg_command)
            and (
                bg_command != command
                or not command_already_backgrounded(command)
            )
        )
        debug_ndjson(
            "H1",
            "workspace_bound_backend.py:execute_bg_decision",
            "long-running / detach decision",
            {
                "os_name": os.name,
                "cwd": str(self.cwd),
                "bg_execute": bg_execute_enabled(),
                "is_server": is_probably_long_running_server(bg_command),
                "already_bg": command_already_backgrounded(command),
                "will_detach": _will_detach,
                "cmd_preview": command[:220].replace("\n", " "),
            },
        )
        # endregion
        if (
            bg_execute_enabled()
            and is_probably_long_running_server(bg_command)
            and (
                bg_command != command
                or not command_already_backgrounded(command)
            )
        ):
            return _finish(
                launch_local_shell_detached(bg_command, cwd=self.cwd, env=self._env)
            )

        try:
            result = subprocess.run(  # noqa: S602
                command,
                check=False,
                shell=True,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                **SUBPROCESS_TEXT_KWARGS,
                timeout=effective_timeout,
                env=self._env,
                cwd=str(self.cwd),
            )
            output_parts = []
            if result.stdout:
                output_parts.append(result.stdout)
            if result.stderr:
                stderr_lines = result.stderr.strip().split("\n")
                output_parts.extend(f"[stderr] {line}" for line in stderr_lines)

            output = "\n".join(output_parts) if output_parts else "<no output>"
            truncated = False
            if len(output) > self._max_output_bytes:
                output = output[: self._max_output_bytes]
                output += f"\n\n... Output truncated at {self._max_output_bytes} bytes."
                truncated = True
            if result.returncode != 0:
                output = f"{output.rstrip()}\n\nExit code: {result.returncode}"

            return _finish(
                ExecuteResponse(
                    output=output,
                    exit_code=result.returncode,
                    truncated=truncated,
                )
            )
        except subprocess.TimeoutExpired:
            if timeout is not None:
                msg = (
                    f"Error: Command timed out after {effective_timeout} seconds "
                    "(custom timeout). The command may be stuck or require more time."
                )
            else:
                msg = (
                    f"Error: Command timed out after {effective_timeout} seconds. "
                    "For long-running commands, re-run using the timeout parameter."
                )
            return _finish(
                ExecuteResponse(
                    output=msg,
                    exit_code=124,
                    truncated=False,
                )
            )
        except Exception as e:  # noqa: BLE001
            return _finish(
                ExecuteResponse(
                    output=f"Error executing command ({type(e).__name__}): {e}",
                    exit_code=1,
                    truncated=False,
                )
            )


__all__ = [
    "WorkspaceBoundLocalShellBackend",
    "_scan_command_for_escape",
    "normalize_workspace_tool_path",
]
