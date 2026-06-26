"""
后台长驻服务启动后的「就绪」探测（跨平台）。

在本地通过 socket 轮询 127.0.0.1 / ::1；在 Linux 沙箱（Docker/Firecracker）内通过
二次 exec 在容器/VM 内连本机监听端口，避免宿主机误测容器 loopback。

环境变量:
- SMARTCLAW_BG_PROBE: 0/false/off 关闭内置 TCP 探测；默认开启。（声明 ``tcp_probe: false`` 可单独关闭内置 TCP）
- SMARTCLAW_BG_PROBE_SECONDS: 最长轮询时间（秒），默认 10；设为 0 则跳过内置 TCP。

声明式自定义检查：``resolve_bg_probe_decl`` + ``execution.bg_probe`` / ``.smartclaw/bg_probe.json``，
字段 ``after_smoke``、``after_smoke_timeout_sec``、``tcp_probe``。
"""

from __future__ import annotations

import base64
import os
import re
import shlex
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from smartclaw.agent.bg_execute import _primary_url_hint
from smartclaw.agent.bg_probe_decl import BgProbeDecl

def bg_probe_enabled() -> bool:
    raw = (os.environ.get("SMARTCLAW_BG_PROBE") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def bg_probe_budget_seconds() -> float:
    raw = (os.environ.get("SMARTCLAW_BG_PROBE_SECONDS") or "10").strip()
    try:
        v = float(raw)
    except ValueError:
        v = 10.0
    return max(0.0, min(v, 120.0))


def _url_to_host_port(url: str) -> tuple[str, int] | None:
    if not url or not url.strip():
        return None
    u = url.strip()
    if "://" not in u:
        u = "http://" + u
    try:
        p = urlparse(u)
    except Exception:
        return None
    host = (p.hostname or "").strip()
    port = p.port
    if not host or port is None:
        return None
    return host, int(port)


def _normalize_loopback_connect_host(host: str) -> str:
    h = host.strip().lower().strip("[]")
    if h in {"", "*", "0.0.0.0", "::", "[::]"}:
        return "127.0.0.1"
    return host.strip()


def _candidate_endpoints(host: str, port: int) -> list[tuple[str, int]]:
    """同一监听语义下，依次尝试的 (host, port)。"""
    h = host.strip().lower().strip("[]")
    if h in {"", "*", "0.0.0.0", "::"}:
        return [("127.0.0.1", port), ("::1", port)]
    if h == "::1":
        return [("::1", port), ("127.0.0.1", port)]
    return [(host, port)]


def _tcp_once(host: str, port: int, timeout_sec: float) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, int(port)), timeout=float(timeout_sec)):
            pass
        return True, ""
    except OSError as e:
        return False, str(e)


def tcp_probe_endpoints(
    endpoints: list[tuple[str, int]],
    *,
    timeout_sec: float,
) -> tuple[bool, str, str]:
    """任一端点连通即成功，返回 (ok, connected_addr, last_error)。"""
    last_err = ""
    for h, p in endpoints:
        ok, err = _tcp_once(h, p, timeout_sec)
        if ok:
            return True, f"{h}:{p}", ""
        last_err = err
    return False, "", last_err


def infer_listen_for_probe(command: str) -> tuple[str, int] | None:
    """
    从命令行推断用于探测的 (connect_host, port)。
    若无法得到端口则返回 None。
    """
    if not command or not isinstance(command, str):
        return None

    hint = _primary_url_hint(command)
    up = _url_to_host_port(hint) if hint else None
    if up:
        ch, pt = up
        return _normalize_loopback_connect_host(ch), pt

    m_bind = re.search(
        r"(?:^|\s)--bind(?:=|\s+)([^\s\"']+):(\d+)\b",
        command,
        re.IGNORECASE,
    )
    if m_bind:
        return _normalize_loopback_connect_host(m_bind.group(1)), int(m_bind.group(2))
    m_uh = re.search(r"(?:^|\s)--host(?:=|\s+)([^\s\"']+)", command, re.IGNORECASE)
    m_up = re.search(r"(?:^|\s)--port(?:=|\s+)(\d+)\b", command, re.IGNORECASE)
    if m_up:
        host = _normalize_loopback_connect_host(m_uh.group(1)) if m_uh else "127.0.0.1"
        return host, int(m_up.group(1))

    m_gu = re.search(
        r"(?:^|\s)(?:-b|--bind)(?:=|\s+)([^\s\"']+):(\d+)\b",
        command,
        re.IGNORECASE,
    )
    if m_gu and re.search(r"\bgunicorn\b", command, re.IGNORECASE):
        return _normalize_loopback_connect_host(m_gu.group(1)), int(m_gu.group(2))

    m_dp = re.search(r"(?:^|\s)-p(?:=|\s+)(\d+)\b", command)
    if m_dp and re.search(r"\bdaphne\b", command, re.IGNORECASE):
        return "127.0.0.1", int(m_dp.group(1))

    m_gr = re.search(
        r"(?:^|\s)--server[-.]port(?:=|\s+)(\d+)\b",
        command,
        re.IGNORECASE,
    )
    if m_gr and re.search(r"\bgradio\b", command, re.IGNORECASE):
        return "127.0.0.1", int(m_gr.group(1))

    if re.search(r"\bmanage\.py\s+runserver\b", command) or re.search(
        r"\bdjango-admin\s+runserver\b",
        command,
        re.IGNORECASE,
    ):
        m_rs = re.search(
            r"\brunserver(?:\s+|-)([0-9.]+|[0-9a-fA-F:]+):(\d+)\b",
            command,
        )
        if m_rs:
            return _normalize_loopback_connect_host(m_rs.group(1)), int(m_rs.group(2))
        m_p = re.search(r"\brunserver(?:\s+|-)(\d+)\s*$", command.strip())
        if m_p:
            return "127.0.0.1", int(m_p.group(1))
        return "127.0.0.1", 8000

    if re.search(r"python(?:3)?\s+-m\s+http\.server\b", command, re.IGNORECASE):
        m_hp = re.search(
            r"(?:^|\s)-m\s+http\.server(?:\s+(\d+))?",
            command,
            re.IGNORECASE,
        )
        if m_hp and m_hp.group(1):
            return "127.0.0.1", int(m_hp.group(1))
        return "127.0.0.1", 8000

    return None


def _opened_addr_to_http_url(addr: str) -> str | None:
    if not addr or ":" not in addr:
        return None
    hostpart, sep, portstr = addr.rpartition(":")
    if not sep or not portstr.isdigit():
        return None
    host = hostpart
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{portstr}/"


def probe_local_listen(
    command: str,
    *,
    budget_seconds: float | None = None,
    interval_sec: float = 0.35,
    per_try_timeout: float = 1.5,
) -> tuple[bool, int, str, str, float]:
    """
    在**当前进程所在机器**上对推断端口做 TCP 轮询。

    返回: (tcp_ok, tries, target_label, last_err_or_http, elapsed_sec)
    """
    budget = bg_probe_budget_seconds() if budget_seconds is None else float(budget_seconds)
    if budget <= 0:
        return False, 0, "", "probe_budget_zero", 0.0

    inferred = infer_listen_for_probe(command)
    if not inferred:
        return False, 0, "", "no_listen_hint", 0.0
    ch, port = inferred
    endpoints = _candidate_endpoints(ch, port)
    target_label = ",".join(f"{h}:{port}" for h, _p in endpoints)

    deadline = time.monotonic() + budget
    tries = 0
    last_err = ""
    t0 = time.monotonic()
    while time.monotonic() < deadline:
        tries += 1
        ok, addr, err = tcp_probe_endpoints(
            endpoints, timeout_sec=per_try_timeout
        )
        if ok:
            elapsed = time.monotonic() - t0
            http_bit = _optional_http_status(command, addr)
            return True, tries, target_label, http_bit, elapsed
        last_err = err
        time.sleep(interval_sec)

    elapsed = time.monotonic() - t0
    return False, tries, target_label, last_err[:180], elapsed


def _optional_http_status(command: str, opened_addr: str) -> str:
    """TCP 已通时尝试一次 HTTP GET，返回简短状态片段（失败不视为 TCP 失败）。"""
    if not opened_addr:
        return ""
    if "http" not in (command or "").lower() and not re.search(
        r"\b(streamlit|uvicorn|flask\s+run|jupyter|gradio|gunicorn|http\.server|runserver)\b",
        command,
        re.IGNORECASE,
    ):
        return ""
    url = _opened_addr_to_http_url(opened_addr)
    if not url:
        return ""
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            return f"http={code}"
    except urllib.error.HTTPError as e:
        return f"http={e.code}"
    except Exception as e:
        return f"http_err={type(e).__name__}"


def format_local_detach_probe_suffix(
    command: str,
    proc: Any | None,
    *,
    tcp_allowed: bool = True,
) -> str:
    """
    供 execute 单行回复末尾拼接：`... | probe=...`

    tcp_allowed：来自声明 ``bg_probe.tcp_probe``；为 False 时不做内置 TCP。
    """
    if not tcp_allowed:
        return "probe=tcp_skipped(decl)"

    if not bg_probe_enabled():
        return "probe=skipped(off)"
    budget = bg_probe_budget_seconds()
    if budget <= 0:
        return "probe=skipped(zero_budget)"

    inferred = infer_listen_for_probe(command)
    if not inferred:
        return "probe=skipped_no_listen_hint"

    ok, tries, tgt, detail, elapsed = probe_local_listen(
        command, budget_seconds=budget
    )

    pid_alive: bool | None = None
    if proc is not None and hasattr(proc, "poll"):
        try:
            pid_alive = proc.poll() is None
        except Exception:
            pid_alive = None

    if ok:
        parts = [
            "probe=tcp_ok",
            f"tries={tries}",
            f"{elapsed:.1f}s",
            f"target={tgt}",
        ]
        if detail:
            parts.append(detail)
        if pid_alive is False:
            parts.append("pid_exited_early")
        return " ".join(parts)

    alive = str(pid_alive) if pid_alive is not None else "unknown"
    err_s = detail.replace("|", ";")[:120]
    return (
        f"probe=tcp_fail tries={tries} {elapsed:.1f}s target={tgt} "
        f"pid_alive={alive} last={err_s!r}"
    )

def build_sandbox_smoke_shell(
    decl: BgProbeDecl,
    *,
    starter_command: str,
    workspace_posix: str,
) -> str:
    """
    Linux 容器/虚拟机内单次 ``bash -lc …``，用于自定义 ``after_smoke``。
    最后一行形如 ``SMOKE_SMARTCLAW exit=N``。
    """
    runner = decl.after_smoke.strip()
    if not runner:
        return ""
    inferred = infer_listen_for_probe(starter_command)
    port_s = str(inferred[1]) if inferred else ""
    ws = workspace_posix.rstrip("/")
    scap = (starter_command or "")[:2000]

    prelude = (
        f"cd {shlex.quote(ws)} && export SMARTCLAW_BG_INFER_PORT={shlex.quote(port_s)} && "
        f"export SMARTCLAW_BG_START_CMD={shlex.quote(scap)} && "
    )
    footer = (
        '; _smartclaw_sm_rc=$?; '
        'printf "%s\\n" "SMOKE_SMARTCLAW exit=$_smartclaw_sm_rc"; '
        "exit $_smartclaw_sm_rc"
    )
    bundle = prelude + runner + footer
    return "bash -lc " + shlex.quote(bundle)


def parse_bg_smoke_line(stdout: str) -> str | None:
    for ln in reversed((stdout or "").replace("\r\n", "\n").strip().splitlines()):
        if "SMOKE_SMARTCLAW" in ln:
            return ln.strip()
    return None


def run_local_after_smoke(
    decl: BgProbeDecl,
    *,
    cwd: Path,
    starter_command: str,
    env_base: dict[str, str] | None,
) -> str:
    """
    宿主工作区内执行自定义 smoke（shell）。
    """
    runner = decl.after_smoke.strip()
    if not runner:
        return ""

    inferred = infer_listen_for_probe(starter_command)
    merged = dict(os.environ.copy() if env_base is None else env_base)
    merged.setdefault("PYTHONUNBUFFERED", "1")
    merged["SMARTCLAW_BG_INFER_PORT"] = str(inferred[1]) if inferred else ""
    merged["SMARTCLAW_BG_START_CMD"] = (starter_command or "")[:8190]

    to = float(min(max(decl.after_smoke_timeout_sec, 1.0), 600.0))
    try:
        proc = subprocess.run(
            runner,
            cwd=str(cwd),
            env=merged,
            shell=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=to,
        )
    except subprocess.TimeoutExpired:
        return f"smoke=timeout after={to:.0f}s"

    ok = proc.returncode == 0
    sniff = ((proc.stderr or "").strip().split("\n"))[-2:] + (
        (proc.stdout or "").strip().split("\n")[-3:]
    )
    snip_raw = "; ".join(s for s in sniff if s)
    snip = snip_raw.replace("|", ";")[:220]
    if ok:
        return f"smoke_ok exit={proc.returncode}"
    return f"smoke_fail exit={proc.returncode} tail={snip!r}"


def build_sandbox_tcp_probe_command(port: int, budget_seconds: float) -> str:
    """
    生成在 Linux 容器/FC 虚拟机内执行的命令字符串（可被 docker/bash -lc 单层引用）。

    优先级（**任一就绪即返回**）:
      1. python3 / python  —— 标准库 socket，最稳定；
      2. bash /dev/tcp     —— 纯 shell 内置，零依赖（适合 nginx/node/distroless 镜像）；
      3. nc -z             —— 经典退路。

    无论走哪条分支，输出末尾都会包含 ``SMARTCLAW_PROBE tcp_ok ...`` 或
    ``SMARTCLAW_PROBE tcp_fail ...``，``parse_sandbox_probe_stdout`` 据此抽行。
    """
    pi = int(port)
    bd = float(min(max(budget_seconds, 0.5), 90.0))

    # === 路径 A：Python（与原实现完全一致，保持现网兼容） ===
    script = (
        "import socket,time\n"
        "deadline=time.time()+"
        + repr(bd)
        + "\nport="
        + str(pi)
        + "\n"
        "tries=0\n"
        "last_err=''\n"
        "targets=[('127.0.0.1',port),('::1',port)]\n"
        "while time.time()<deadline:\n"
        " tries+=1\n"
        " for host,p in targets:\n"
        "  try:\n"
        "   s=socket.create_connection((host,p),2.0)\n"
        "   s.close()\n"
        "   msg='SMARTCLAW_PROBE tcp_ok tries='+str(tries)"
        "+' target='+host+':'+str(p);print(msg)\n"
        "   raise SystemExit(0)\n"
        "  except OSError as e:\n"
        "   last_err=str(e)\n"
        " time.sleep(0.35)\n"
        "msg=('SMARTCLAW_PROBE tcp_fail tries='+"
        "str(tries)+' last='+last_err[:180]);print(msg)\n"
        "raise SystemExit(2)\n"
    )
    b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
    py = "import base64; exec(base64.b64decode(" + repr(b64) + "))"
    qpy = shlex.quote(py)

    # === 路径 B：bash /dev/tcp（无须安装任何额外二进制） ===
    # 估算 try 次数：每次 ~0.5s（connect 超时 + sleep 0.35）。
    max_tries = max(1, int(bd / 0.5) + 1)
    bash_script = (
        "set +e; "
        f"deadline=$(( $(date +%s) + {int(bd) + 1} )); "
        f"port={pi}; tries=0; last=''; "
        f"while [ \"$(date +%s)\" -lt \"$deadline\" ] && [ \"$tries\" -lt {max_tries} ]; do "
        "tries=$((tries+1)); "
        "for host in 127.0.0.1 ::1; do "
        "  if (exec 3<>/dev/tcp/$host/$port) 2>/dev/null; then "
        "    exec 3>&- 3<&-; "
        "    printf 'SMARTCLAW_PROBE tcp_ok tries=%s target=%s:%s\\n' \"$tries\" \"$host\" \"$port\"; "
        "    exit 0; "
        "  fi; "
        "done; "
        "last=\"connection refused\"; sleep 0.35; "
        "done; "
        "printf 'SMARTCLAW_PROBE tcp_fail tries=%s last=%s\\n' \"$tries\" \"$last\"; exit 2"
    )
    qbash = shlex.quote(bash_script)

    # === 路径 C：nc -z（busybox 也常自带） ===
    nc_script = (
        "set +e; "
        f"deadline=$(( $(date +%s) + {int(bd) + 1} )); "
        f"port={pi}; tries=0; "
        f"while [ \"$(date +%s)\" -lt \"$deadline\" ] && [ \"$tries\" -lt {max_tries} ]; do "
        "tries=$((tries+1)); "
        "for host in 127.0.0.1 ::1; do "
        "  if nc -z -w 1 $host $port >/dev/null 2>&1; then "
        "    printf 'SMARTCLAW_PROBE tcp_ok tries=%s target=%s:%s\\n' \"$tries\" \"$host\" \"$port\"; "
        "    exit 0; "
        "  fi; "
        "done; sleep 0.35; "
        "done; "
        "printf 'SMARTCLAW_PROBE tcp_fail tries=%s last=nc_unreachable\\n' \"$tries\"; exit 2"
    )
    qnc = shlex.quote(nc_script)

    # 注意：``DockerSandboxBackend.execute`` 外层固定 ``bash -c``，因此 ``$BASH_VERSION``
    # 在容器里几乎总是有值；这里同时检查 ``command -v bash`` 是为了兼容
    # Firecracker / 自定义外层（``sh -c``）的场景。
    return (
        f"if command -v python3 >/dev/null 2>&1; then python3 -c {qpy}; "
        f"elif command -v python >/dev/null 2>&1; then python -c {qpy}; "
        f"elif [ -n \"$BASH_VERSION\" ] || command -v bash >/dev/null 2>&1; then "
        f"bash -c {qbash}; "
        f"elif command -v nc >/dev/null 2>&1; then sh -c {qnc}; "
        f"else printf 'SMARTCLAW_PROBE tcp_fail tries=0 last=no_probe_tool\\n'; exit 3; fi"
    )


def parse_sandbox_probe_stdout(stdout: str) -> str | None:
    for line in (stdout or "").splitlines():
        if "SMARTCLAW_PROBE" in line:
            return line.strip()
    return None


__all__ = [
    "bg_probe_budget_seconds",
    "bg_probe_enabled",
    "build_sandbox_smoke_shell",
    "build_sandbox_tcp_probe_command",
    "format_local_detach_probe_suffix",
    "infer_listen_for_probe",
    "parse_bg_smoke_line",
    "parse_sandbox_probe_stdout",
    "probe_local_listen",
    "run_local_after_smoke",
]