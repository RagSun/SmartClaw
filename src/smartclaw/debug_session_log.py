"""Cursor debug session NDJSON sink; remove after verification (session 89f6ca)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

_SESSION_ID = "89f6ca"
_LOG_NAME = "debug-89f6ca.log"


def _log_path() -> Path:
    # .../dev/SmartClaw_0404/src/smartclaw/debug_session_log.py -> parents[3] == dev
    return Path(__file__).resolve().parents[3] / _LOG_NAME


def debug_ndjson(
    hypothesis_id: str,
    location: str,
    message: str,
    data: dict[str, Any] | None = None,
) -> None:
    # region agent log
    try:
        payload = {
            "sessionId": _SESSION_ID,
            "timestamp": int(time.time() * 1000),
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data or {},
        }
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass
    # endregion agent log
