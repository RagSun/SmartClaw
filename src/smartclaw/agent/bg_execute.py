"""
长驻前台服务类命令（如 streamlit run）在 execute 中会阻塞直至超时。
本模块在本地 / Linux 容器内以跨平台方式改为后台启动，立即返回 PID、日志路径与 URL 提示。

后台启动后的 TCP/HTTP 就绪探测见 agent/bg_probe.py；
环境变量 SMARTCLAW_BG_PROBE（默认开）、SMARTCLAW_BG_PROBE_SECONDS（默认 10）。

声明式自定义检测（任意框架）：``agent.json`` 的 ``execution.bg_probe``（或顶层 ``bg_probe``）
与工作区 ``.smartclaw/bg_probe.json``（后者覆盖前者）。字段：
``after_smoke`` shell 命令；``after_smoke_timeout_sec``；``tcp_probe`` 是否启用内置 TCP（默认 true）。
执行 ``after_smoke`` 时注入 ``SMARTCLAW_BG_INFER_PORT``、``SMARTCLAW_BG_START_CMD``。
"""

from __future__ import annotations

import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from deepagents.backends.protocol import ExecuteResponse

from smartclaw.agent.bg_registry import register_local_task
from smartclaw.auth.tool_gate import get_tool_security_context
from smartclaw.debug_session_log import debug_ndjson

# 与 Docker/Firecracker 内工作区常量保持一致（须与 DockerSandboxBackend 挂载目标一致）
# 实际值取自 config [sandbox].container_workspace；此处仅为兜底默认。
SANDBOX_WORKSPACE_POSIX = "/workspace"

_LONG_RUNNING_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        # ===== Python：CLI 入口 =====
        r"\bstreamlit\s+run\b",
        r"\bjupyter\s+(?:notebook|lab|server)\b",
        r"\buvicorn\b",
        r"\bgunicorn\b",
        r"\bhypercorn\b",
        r"\bdaphne\b",
        r"\bgradio\b",
        r"python(?:3)?\s+-m\s+http\.server\b",
        r"python(?:3)?\s+-m\s+uvicorn\b",
        r"python(?:3)?\s+-m\s+gunicorn\b",
        r"python(?:3)?\s+-m\s+hypercorn\b",
        r"python(?:3)?\s+-m\s+flask\b",
        r"python(?:3)?\s+-m\s+streamlit\b",
        r"python(?:3)?\s+-m\s+gradio\b",
        r"\bflask\s+(?:run|--app\b)",
        r"\bfastapi\s+(?:dev|run)\b",
        r"\bbentoml\s+serve\b",
        r"\bmkdocs\s+serve\b",
        r"\bdjango-admin\s+runserver\b",
        r"\bmanage\.py\s+runserver\b",
        r"\buv\s+run\s+(?:uvicorn|gunicorn|hypercorn|fastapi|flask|streamlit|gradio)\b",
        r"\bcelery\s+(?:-A\s+\S+\s+)?(?:worker|beat|flower)\b",
        r"\brq\s+worker\b",
        # 常见「python app.py / hello_flask.py」启动内嵌 app.run()；原先只匹配 flask CLI，导致前台阻塞或超时、端口无监听
        r"\bpython(?:3)?\s+\S*app\.py\b",
        r"\bpython(?:3)?\s+\S*main\.py\b",
        r"\bpython(?:3)?\s+\S*server\.py\b",
        r"\bpython(?:3)?\s+\S*application\.py\b",
        r"\bpython(?:3)?\s+\S*wsgi\.py\b",
        r"\bpython(?:3)?\s+\S*asgi\.py\b",
        r"\bpython(?:3)?\s+\S*hello\w*\.py\b",
        # 习惯性命名的自启动脚本（run/start/serve/web/api/bot/launch）
        r"\bpython(?:3)?\s+\S*(?:^|/|\\)?(?:run|start|serve|web|api|bot|launch)\.py\b",
        # ===== Node / 前端开发服务器 =====
        r"\bnode\s+\S*(?:server|app|index|main)\.[cm]?js\b",
        r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:dev|start|serve)\b",
        r"\b(?:next|nuxt|remix|astro|vite|webpack|parcel|rollup)\s+(?:dev|start|serve)\b",
        r"\bvite\b(?!\s*build)",
        r"\bnpx\s+(?:vite|next|nuxt|serve|http-server|live-server)\b",
        r"\bserve\s+(?:-s\s+)?(?:build|dist|public|out)\b",
        # ===== 其它语言 / 通用 =====
        r"\bgo\s+run\b",
        r"\brails\s+server\b",
        r"\brake\s+server\b",
        r"\bphp\s+-S\b",
        r"\bcaddy\s+(?:run|start)\b",
        r"\bnginx\b(?!\s+(?:-t|-s|-v|-V))",
        r"\bredis-server\b",
        r"\btail\s+-f\b",
    )
)

_BG_FLAG_ENV = "SMARTCLAW_BG_EXECUTE"


def bg_execute_enabled() -> bool:
    v = (os.environ.get(_BG_FLAG_ENV) or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


# 「内联 Python 启动 HTTP 服务」启发：
# 命令字符串里同时出现 ``python(3)?`` 以及任何下面的"服务启动调用"片段，即视为长驻。
# 覆盖典型反模式:
#   python3 -c "from flask import Flask; app=Flask(...); app.run(host='0.0.0.0', port=N)"
#   python3 << 'PY' \n from fastapi import FastAPI \n uvicorn.run(app, ...) \n PY
#   python3 /tmp/x.py  (但里面又是 Flask/FastAPI app.run 启动 — 这条无法静态判断，跳过)
_INLINE_PYTHON_SERVER_HINTS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        # 同行同时匹配 python 解释器 + 服务启动调用
        r"python(?:3)?\b.*\bFlask\s*\(",
        r"python(?:3)?\b.*\bFastAPI\s*\(",
        r"python(?:3)?\b.*\bapp\.run\s*\(",
        r"python(?:3)?\b.*\buvicorn\.run\s*\(",
        r"python(?:3)?\b.*\bgradio\.(?:launch|Interface)\s*\(",
        r"python(?:3)?\b.*\bgr\.(?:Interface|Blocks)\(.*\)\.launch\s*\(",
        r"python(?:3)?\b.*\bserve_forever\s*\(",
        r"python(?:3)?\b.*\bhttp\.server\.ThreadingHTTPServer\b",
        r"python(?:3)?\b.*\bsocketserver\.TCPServer\s*\(",
    )
)


def _inline_python_server_command(command: str) -> bool:
    if not command:
        return False
    return any(p.search(command) for p in _INLINE_PYTHON_SERVER_HINTS)


def is_probably_long_running_server(command: str) -> bool:
    if not command or not isinstance(command, str):
        return False
    if any(p.search(command) for p in _LONG_RUNNING_PATTERNS):
        return True
    # 旁路启发：覆盖「python -c "...Flask(__name__) ... app.run(...)"」等
    # 内联启动模式（_LONG_RUNNING_PATTERNS 只能看命令开头，无法窥探脚本字符串内部）
    return _inline_python_server_command(command)


def command_already_backgrounded(command: str) -> bool:
    """用户已使用常见后台语法时不再二次包裹。"""
    s = command.strip()
    if s.endswith("&"):
        return True
    if re.search(r"\bnohup\s+", command, re.IGNORECASE):
        return True
    if os.name == "nt" and re.search(r"\bstart\s+/[Bb]\b", command):
        return True
    return False


def unwrap_windows_start_background(command: str) -> str:
    """
    将 `start /B <server command>` 还原为真实长驻服务命令。

    DeepAgents 在 Windows 上常会自己加 `start /B`，这会让 shell 立即返回空输出，
    但平台也拿不到 `[bg] log=... | pid=... | URL`。对这类命令先拆壳，再交给
    `launch_local_shell_detached()` 统一后台化。
    """
    if os.name != "nt" or not command or not isinstance(command, str):
        return command

    m = re.match(r"(?is)^\s*start\s+(?P<rest>.+?)\s*$", command)
    if not m:
        return command

    rest = m.group("rest").lstrip()
    saw_background_flag = False
    while True:
        opt = re.match(r"(?is)^/([A-Za-z]+)(?:\s+|$)", rest)
        if not opt:
            break
        if opt.group(1).lower() == "b":
            saw_background_flag = True
        rest = rest[opt.end() :].lstrip()

    if not saw_background_flag or not rest:
        return command

    title = re.match(r'(?s)^"[^"]*"\s+(.+)$', rest)
    if title:
        rest = title.group(1).lstrip()
    return rest


def _sh_single_quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


# ------------------------------------------------------------------
# heredoc \n 还原（沙箱前置处理）
#
# 现象：模型在工具调用 JSON 里写 ``cat > x.py << 'PYEOF'\nfrom flask ...\nPYEOF``，
# 序列化/反序列化链路在某些情况下会把 ``\n`` 保留为字面"反斜杠+n"两个字符；
# 容器内 bash 读到 ``<< 'PYEOF'`` 后再也找不到独立成行的 ``PYEOF``，heredoc 永
# 远不闭合，命令以 ``syntax error: unexpected end of file`` (exit=2) 失败。
#
# 处理策略（保守、可独立关闭）：
# - 仅在「同时满足」以下条件时改写，避免误伤合法命令：
#     a) 命令字符串里包含 ``<< [-]['"]?EOF_MARK['"]?``（heredoc 起始）；
#     b) 命令字符串中**没有任何真实换行符**（即一切已被 squash 成单行）；
#     c) 字符串里至少有一个字面 ``\n`` 或 ``\r``、``\t``。
# - 改写：将 ``\n``、``\r``、``\t`` 还原为真换行 / 回车 / Tab。
# - 关闭方式：``SMARTCLAW_HEREDOC_UNESCAPE=0``。
# ------------------------------------------------------------------

_HEREDOC_START_RE = re.compile(
    r"<<-?\s*(['\"]?)(?P<mark>[A-Za-z_][A-Za-z0-9_]*)\1",
)


def heredoc_unescape_enabled() -> bool:
    v = (os.environ.get("SMARTCLAW_HEREDOC_UNESCAPE") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def maybe_unescape_heredoc_newlines(command: str) -> str:
    """检测 cat/python heredoc 被 escape 成字面 ``\\n`` 的情形，按需还原为真换行。

    返回值始终为字符串（不变形时原样返回）。**永不抛异常**；处理失败回退原值。
    """
    if not heredoc_unescape_enabled():
        return command
    if not command or not isinstance(command, str):
        return command
    # 必须命中 heredoc 起始
    m = _HEREDOC_START_RE.search(command)
    if not m:
        return command
    # 字符串本身已经包含真换行 → 模型/上游已正确传 newline，不再二次处理。
    # 这里只针对"整条命令被 squash 成单行 + 字面 \n"的退化场景。
    if "\n" in command or "\r" in command:
        return command
    # 必须包含至少一个字面 \n / \r / \t；否则没什么可修
    if not any(esc in command for esc in ("\\n", "\\r", "\\t")):
        return command
    try:
        rewritten = (
            command.replace("\\r\\n", "\n")
            .replace("\\n", "\n")
            .replace("\\r", "\n")
            .replace("\\t", "\t")
        )
        # 还原后，heredoc 终结符 EOF_MARK 必须以独立一行出现，否则反不如不改
        mark = m.group("mark")
        if mark and not re.search(rf"(^|\n){re.escape(mark)}(\s*$|\s*\n)", rewritten):
            return command
        return rewritten
    except Exception:
        return command


def _parse_host_port(command: str) -> tuple[str, int | None]:
    host = "127.0.0.1"
    port: int | None = None
    hm = re.search(r"--server\.address(?:=|\s+)([^\s\"']+)", command)
    if hm:
        host = hm.group(1).strip().strip('"').strip("'")
    pm = re.search(r"--server\.port(?:=|\s+)(\d+)", command)
    if pm:
        port = int(pm.group(1))
    return host, port


def _parse_uvicorn_host_port(command: str) -> tuple[str, int | None]:
    """解析 uvicorn 常见参数 (--bind / --host + --port)。"""
    m_bind = re.search(
        r"(?:^|\s)--bind(?:=|\s+)([^\s\"']+):(\d+)\b",
        command,
        re.IGNORECASE,
    )
    if m_bind:
        host = m_bind.group(1).strip().strip('"').strip("'")
        return host, int(m_bind.group(2))
    hm = re.search(r"(?:^|\s)--host(?:=|\s+)([^\s\"']+)", command, re.IGNORECASE)
    pm = re.search(r"(?:^|\s)--port(?:=|\s+)(\d+)\b", command, re.IGNORECASE)
    if pm:
        host = hm.group(1).strip().strip('"').strip("'") if hm else "127.0.0.1"
        return host, int(pm.group(1))
    return "127.0.0.1", None


def _primary_url_hint(command: str) -> str:
    """从命令行解析出的首选访问 URL；无法解析则返回空字符串。"""
    host, port = _parse_host_port(command)
    if re.search(r"\bstreamlit\s+run\b", command, re.IGNORECASE):
        p = port if port is not None else 8501
        return f"http://{host}:{p}"
    if re.search(r"\buvicorn\b", command, re.IGNORECASE):
        uh, up = _parse_uvicorn_host_port(command)
        p = up if up is not None else 8000
        return f"http://{uh}:{p}"
    if re.search(r"python(?:3)?\s+-m\s+http\.server\b", command, re.IGNORECASE):
        p = port if port is not None else 8000
        return f"http://{host}:{p}"
    if re.search(r"\bjupyter\s+lab\b", command, re.IGNORECASE):
        p = port if port is not None else 8888
        return f"http://{host}:{p}"
    if re.search(r"\bjupyter\s+notebook\b", command, re.IGNORECASE):
        p = port if port is not None else 8888
        return f"http://{host}:{p}"
    if re.search(r"\bflask\s+run\b", command, re.IGNORECASE):
        fh = "127.0.0.1"
        fp: int | None = None
        hm = re.search(r"(?:^|\s)--host(?:=|\s+)([^\s\"']+)", command, re.IGNORECASE)
        if hm:
            fh = hm.group(1).strip().strip('"').strip("'")
        pm = re.search(r"(?:^|\s)(?:--port|-p)(?:=|\s+)(\d+)", command, re.IGNORECASE)
        if pm:
            fp = int(pm.group(1))
        p = fp if fp is not None else 5000
        return f"http://{fh}:{p}"
    return ""


def format_compact_background_tool_reply(
    command: str,
    *,
    rel_log: str,
    pid: int | None = None,
    task_id: str | None = None,
) -> str:
    """execute 返回给模型的单行核心信息（日志路径 / pid / URL 或查日志指令）。"""
    url = _primary_url_hint(command)
    tail = url if url else "grep -i http .smartclaw_bg/*.log"
    prefix = f"[bg] id={task_id} | log={rel_log}" if task_id else f"[bg] log={rel_log}"
    if pid is not None:
        return f"{prefix} | pid={pid} | {tail}"
    return f"{prefix} | {tail}"


def compact_background_url_suffix(command: str) -> str:
    """容器内已有 log 行时，仅追加一行 URL 或 grep 提示。"""
    url = _primary_url_hint(command)
    if url:
        return f"[bg] {url}"
    return "[bg] grep -i http .smartclaw_bg/*.log"


def server_url_hints_text(command: str) -> str:
    """兼容旧调用：改为单行核心提示。"""
    url = _primary_url_hint(command)
    return url if url else "grep -i http .smartclaw_bg/*.log"


def wrap_command_linux_container(
    command: str,
    *,
    workspace_posix: str,
    log_relpath: str,
) -> str:
    """
    在 Linux 容器 / 沙箱内用 nohup 启动，shell 立即退出（exit 0）。
    log_relpath 为相对于 workspace 根的路径，例如 .smartclaw_bg/x.log
    """
    ws = workspace_posix.rstrip("/")
    log_full = f"{ws}/{log_relpath.lstrip('/')}"
    bg_dir = f"{ws}/.smartclaw_bg"
    inner = _sh_single_quote(command)
    ack = f"bg ok {log_relpath}"
    return (
        f"mkdir -p {_sh_single_quote(bg_dir)} && "
        f"nohup env PYTHONUNBUFFERED=1 sh -c {inner} >> {_sh_single_quote(log_full)} 2>&1 & "
        f"printf '%s\\n' {_sh_single_quote(ack)}"
    )


def launch_local_shell_detached(
    command: str,
    *,
    cwd: Path,
    env: dict[str, str] | None,
) -> ExecuteResponse:
    """Windows / macOS / Linux 本地：子进程脱离会话，立即返回。"""
    bg_dir = cwd / ".smartclaw_bg"
    try:
        bg_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return ExecuteResponse(
            output=f"无法创建后台日志目录 {bg_dir}: {e}",
            exit_code=1,
            truncated=False,
        )

    log_path = bg_dir / f"bg_{uuid.uuid4().hex[:12]}.log"

    if env is not None:
        child_env = dict(env)
        child_env.setdefault("PYTHONUNBUFFERED", "1")
    else:
        child_env = os.environ.copy()
        child_env.setdefault("PYTHONUNBUFFERED", "1")

    popen_kw: dict[str, object] = {
        "shell": True,
        "stdin": subprocess.DEVNULL,
        "cwd": str(cwd),
        "env": child_env,
    }
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        popen_kw["creationflags"] = flags
    else:
        popen_kw["start_new_session"] = True

    try:
        with open(log_path, "a", encoding="utf-8", errors="replace") as log_f:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            log_f.write(
                f"[smartclaw-bg] {ts} starting detached process\n"
                f"[smartclaw-bg] cwd={cwd}\n"
                f"[smartclaw-bg] command={command}\n"
                f"[smartclaw-bg] hint: execute tool stdout already includes [bg] URL / pid; "
                "service logs may lag until buffers flush (PYTHONUNBUFFERED=1 is set).\n"
            )
            log_f.flush()
            proc = subprocess.Popen(  # noqa: S603,S607
                command,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                **popen_kw,
            )
    except OSError as e:
        return ExecuteResponse(
            output=f"后台启动失败 ({type(e).__name__}): {e}",
            exit_code=1,
            truncated=False,
        )

    try:
        rel_log = log_path.relative_to(cwd)
    except ValueError:
        rel_log = log_path
    url = _primary_url_hint(command)
    tctx = get_tool_security_context()
    task = register_local_task(
        command=command,
        cwd=cwd,
        log_path=log_path,
        rel_log=str(rel_log),
        process=proc,
        url=url,
        tenant_id=getattr(tctx, "tenant_id", "default") if tctx else "default",
        agent_id=getattr(tctx, "agent_id", "") if tctx else "",
    )
    try:
        with open(log_path, "a", encoding="utf-8", errors="replace") as log_f:
            log_f.write(f"[smartclaw-bg] task_id={task.id}\n")
            log_f.flush()
    except OSError:
        pass
    msg = format_compact_background_tool_reply(
        command, rel_log=str(rel_log), pid=proc.pid, task_id=task.id
    )
    from smartclaw.agent.bg_probe import (
        format_local_detach_probe_suffix,
        run_local_after_smoke,
    )
    from smartclaw.agent.bg_probe_decl import resolve_bg_probe_decl
    from smartclaw.agent.tools.exec_context import get_agent_config_for_exec

    decl = resolve_bg_probe_decl(
        agent_cfg=get_agent_config_for_exec(),
        workspace_root=cwd,
    )
    msg = (
        f"{msg} | {format_local_detach_probe_suffix(command, proc, tcp_allowed=decl.tcp_probe)}"
    )
    smoke_rest = run_local_after_smoke(
        decl,
        cwd=cwd,
        starter_command=command,
        env_base=child_env,
    )
    if smoke_rest:
        msg = f"{msg} | {smoke_rest}"
    # region agent log
    debug_ndjson(
        "H-C",
        "bg_execute.py:launch_local_shell_detached",
        "detached server process started",
        {
            "pid": proc.pid,
            "cwd": str(cwd),
            "url_hint": url,
            "rel_log": str(rel_log),
            "msg_out": msg[:500],
            "cmd_preview": command[:260].replace("\n", " "),
        },
    )
    # endregion
    return ExecuteResponse(output=msg, exit_code=0, truncated=False)


__all__ = [
    "SANDBOX_WORKSPACE_POSIX",
    "bg_execute_enabled",
    "command_already_backgrounded",
    "compact_background_url_suffix",
    "format_compact_background_tool_reply",
    "heredoc_unescape_enabled",
    "is_probably_long_running_server",
    "launch_local_shell_detached",
    "maybe_unescape_heredoc_newlines",
    "server_url_hints_text",
    "unwrap_windows_start_background",
    "wrap_command_linux_container",
]
