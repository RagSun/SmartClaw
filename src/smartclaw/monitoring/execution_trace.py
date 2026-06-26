"""
执行链路审计：planner / skill / tool / fallback 等事件的 JSONL 落盘。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Optional


def trace_root() -> Path:
    p = Path.home() / ".smartclaw" / "execution-trace"
    p.mkdir(parents=True, exist_ok=True)
    return p


def new_trace_id() -> str:
    return str(uuid.uuid4())


def record_execution_event(
    *,
    event_type: str,
    trace_id: str,
    agent_id: str,
    session_id: str,
    tenant_id: str,
    data: Optional[dict[str, Any]] = None,
    emit: bool = True,
) -> None:
    if not emit:
        return
    row = {
        "ts": time.time(),
        "event_type": event_type,
        "trace_id": trace_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "tenant_id": tenant_id,
        "data": data or {},
    }
    log_path = trace_root() / "events.jsonl"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
