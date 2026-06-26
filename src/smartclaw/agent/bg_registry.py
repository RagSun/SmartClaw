"""SmartClaw 后台任务注册表。

用于记录由 execute 自动后台化的长驻服务，提供 list/status/log/kill 等统一入口。
注册表仅保存在当前 SmartClaw 进程内；服务重启后历史后台任务不会恢复。
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smartclaw.subprocess_io import SUBPROCESS_TEXT_KWARGS


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class BackgroundTask:
    id: str
    command: str
    cwd: str
    log_path: str
    rel_log: str
    url: str = ""
    tenant_id: str = "default"
    agent_id: str = ""
    pid: int | None = None
    backend: str = "local"
    started_at: str = field(default_factory=_utc_now_iso)
    ended_at: str | None = None
    exit_code: int | None = None
    status: str = "running"
    _process: subprocess.Popen[Any] | None = field(default=None, repr=False, compare=False)

    def refresh(self) -> None:
        if self.status not in ("running", "unknown"):
            return
        if self._process is None:
            return
        code = self._process.poll()
        if code is None:
            self.status = "running"
            return
        self.exit_code = code
        self.ended_at = self.ended_at or _utc_now_iso()
        self.status = "completed" if code == 0 else "failed"

    def to_dict(self, *, include_command: bool = True) -> dict[str, Any]:
        self.refresh()
        data: dict[str, Any] = {
            "id": self.id,
            "status": self.status,
            "backend": self.backend,
            "pid": self.pid,
            "url": self.url,
            "tenant_id": self.tenant_id,
            "agent_id": self.agent_id,
            "rel_log": self.rel_log,
            "cwd": self.cwd,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "exit_code": self.exit_code,
        }
        if include_command:
            data["command"] = self.command
        return data


_lock = threading.RLock()
_tasks: dict[str, BackgroundTask] = {}


def register_local_task(
    *,
    command: str,
    cwd: Path,
    log_path: Path,
    rel_log: str,
    process: subprocess.Popen[Any],
    url: str = "",
    tenant_id: str = "default",
    agent_id: str = "",
) -> BackgroundTask:
    task_id = f"bg_{uuid.uuid4().hex[:10]}"
    task = BackgroundTask(
        id=task_id,
        command=command,
        cwd=str(cwd),
        log_path=str(log_path),
        rel_log=rel_log,
        url=url,
        tenant_id=tenant_id,
        agent_id=agent_id,
        pid=process.pid,
        backend="local",
        _process=process,
    )
    with _lock:
        _tasks[task.id] = task
    return task


def list_tasks(
    include_finished: bool = True,
    *,
    tenant_id: str | None = None,
    agent_id: str | None = None,
) -> list[dict[str, Any]]:
    """List tasks, optionally filtered by tenant and agent."""
    with _lock:
        tasks = list(_tasks.values())
    out: list[dict[str, Any]] = []
    for task in tasks:
        task.refresh()
        if tenant_id and task.tenant_id != tenant_id:
            continue
        if agent_id and task.agent_id and task.agent_id != agent_id:
            continue
        if include_finished or task.status == "running":
            out.append(task.to_dict())
    out.sort(key=lambda item: str(item.get("started_at") or ""), reverse=True)
    return out


def get_task(task_id: str) -> BackgroundTask | None:
    with _lock:
        task = _tasks.get(task_id)
    if task:
        task.refresh()
    return task


def remove_task(task_id: str) -> bool:
    with _lock:
        return _tasks.pop(task_id, None) is not None


def read_task_log(task_id: str, *, lines: int = 80) -> dict[str, Any]:
    task = get_task(task_id)
    if not task:
        return {"success": False, "error": f"后台任务不存在: {task_id}"}
    path = Path(task.log_path)
    if not path.exists():
        return {
            "success": False,
            "error": f"日志文件不存在: {task.rel_log}",
            "task": task.to_dict(),
        }
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"success": False, "error": f"读取日志失败: {e}", "task": task.to_dict()}

    raw_lines = text.splitlines()
    n = max(1, min(int(lines or 80), 1000))
    tail_lines = raw_lines[-n:]
    return {
        "success": True,
        "task": task.to_dict(),
        "total_lines": len(raw_lines),
        "shown_lines": len(tail_lines),
        "output": "\n".join(tail_lines) if tail_lines else "(no output yet)",
    }


def kill_task(task_id: str) -> dict[str, Any]:
    task = get_task(task_id)
    if not task:
        return {"success": False, "error": f"后台任务不存在: {task_id}"}
    if task.status != "running":
        return {"success": True, "message": f"任务已结束: {task.status}", "task": task.to_dict()}
    if not task.pid:
        return {"success": False, "error": "任务没有可终止的 pid", "task": task.to_dict()}

    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(task.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
                **SUBPROCESS_TEXT_KWARGS,
            )
        else:
            try:
                os.killpg(task.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError:
                os.kill(task.pid, signal.SIGTERM)
        task.status = "killed"
        task.ended_at = task.ended_at or _utc_now_iso()
        return {"success": True, "message": f"已终止后台任务 {task_id}", "task": task.to_dict()}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": f"终止任务失败: {e}", "task": task.to_dict()}


__all__ = [
    "BackgroundTask",
    "get_task",
    "kill_task",
    "list_tasks",
    "read_task_log",
    "register_local_task",
    "remove_task",
]
