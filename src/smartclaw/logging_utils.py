"""Logging helpers for safe operational diagnostics."""

from __future__ import annotations

import json
import re
from typing import Any


_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"tvly-[A-Za-z0-9_-]{12,}"), "tvly-<REDACTED>"),
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "sk-<REDACTED>"),
    (re.compile(r"AKIA[0-9A-Z]{12,}"), "AKIA<REDACTED>"),
    (re.compile(r"(?i)(api[_ -]?key|apikey|token|secret|password|authorization)(\s*[:=]\s*)(['\"]?)[^\s,'\"}]{6,}"), r"\1\2\3<REDACTED>"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"), r"\1<REDACTED>"),
)


_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "app_secret",
    "authorization",
    "bearer",
    "password",
    "secret",
    "token",
}


def redact_text(value: Any) -> str:
    """Return a string with common credentials redacted."""
    text = str(value)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def safe_preview(value: Any, limit: int = 96) -> str:
    """Redact then compact a value for log previews."""
    text = redact_text(value).replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def summarize_payload(value: Any, *, max_value_len: int = 80) -> str:
    """Summarize tool parameters/results without leaking sensitive values."""
    def _scrub(obj: Any) -> Any:
        if isinstance(obj, dict):
            out: dict[str, Any] = {}
            for key, val in obj.items():
                k = str(key)
                if k.lower() in _SENSITIVE_KEYS or any(part in k.lower() for part in ("secret", "token", "password", "api_key")):
                    out[k] = "<REDACTED>"
                elif isinstance(val, (dict, list, tuple)):
                    out[k] = _scrub(val)
                else:
                    out[k] = safe_preview(val, max_value_len)
            return out
        if isinstance(obj, (list, tuple)):
            return [_scrub(item) for item in list(obj)[:8]]
        return safe_preview(obj, max_value_len)

    try:
        return json.dumps(_scrub(value), ensure_ascii=False, default=str)
    except Exception:
        return safe_preview(value, max_value_len)


__all__ = ["redact_text", "safe_preview", "summarize_payload"]
