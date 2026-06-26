"""
租户感知的 HTTP 请求工具：自动合并 auth.tenant_integration_env 中的请求头。

键名即 HTTP Header 名（如 Authorization、X-Api-Key），值即头内容。
用于调用 OMS 等后端集成接口（密码仍放在服务端配置，勿交给模型）。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from smartclaw.auth.tool_gate import get_tenant_integration_env
from smartclaw.console import error, info


def _normalize_headers(extra: dict[str, str]) -> dict[str, str]:
    return {str(k).strip(): str(v) for k, v in extra.items() if str(k).strip()}


async def integration_http_request(
    method: str,
    url: str,
    headers_json: str = "",
    body: str = "",
    timeout_seconds: float = 30.0,
) -> str:
    """
    对 url 发起 HTTP 请求；自动附加当前租户在 tenant_integration_env 中配置的 Header。

    参数:
        method: GET / POST / PUT / PATCH / DELETE（大小写不敏感）
        url: 完整 URL
        headers_json: 可选，JSON 对象字符串，额外请求头（会与租户头合并，同名时额外头优先）
        body: POST/PUT/PATCH 的请求体字符串（原样发送）
        timeout_seconds: 超时
    """
    m = (method or "GET").upper()
    tenant_hdrs = _normalize_headers(get_tenant_integration_env())
    extra: dict[str, str] = {}
    if headers_json and headers_json.strip():
        try:
            raw = json.loads(headers_json)
            if isinstance(raw, dict):
                extra = _normalize_headers({str(k): str(v) for k, v in raw.items()})
        except json.JSONDecodeError as e:
            return json.dumps({"ok": False, "error": f"headers_json 非合法 JSON: {e}"}, ensure_ascii=False)

    merged = {**tenant_hdrs, **extra}
    info(f"[integration_http] {m} {url[:80]}... headers={list(merged.keys())}")

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            if m == "GET":
                r = await client.get(url, headers=merged if merged else None)
            elif m == "DELETE":
                r = await client.delete(url, headers=merged if merged else None)
            elif m in ("POST", "PUT", "PATCH"):
                r = await client.request(m, url, headers=merged if merged else None, content=body or None)
            else:
                return json.dumps({"ok": False, "error": f"不支持的方法: {method}"}, ensure_ascii=False)
            text = r.text
            if len(text) > 120_000:
                text = text[:120_000] + "\n...[truncated]"
            return json.dumps(
                {
                    "ok": r.is_success,
                    "status_code": r.status_code,
                    "body": text,
                },
                ensure_ascii=False,
            )
    except Exception as e:
        error(f"integration_http_request 失败: {e}")
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


async def integration_http_request_handler(
    method: str,
    url: str,
    headers_json: str = "",
    body: str = "",
    timeout_seconds: float = 30.0,
) -> str:
    return await integration_http_request(method, url, headers_json, body, timeout_seconds)
