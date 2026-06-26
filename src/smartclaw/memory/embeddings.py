"""Embedding provider client for memory hybrid search."""

from __future__ import annotations

import os
from typing import Any

import httpx


class EmbeddingError(RuntimeError):
    """Raised when embedding generation fails."""


def _clean_base_url(base_url: str) -> str:
    return (base_url or "").strip().rstrip("/")


def embed_texts_openai_compatible(
    *,
    texts: list[str],
    api_key: str,
    base_url: str,
    model: str,
    dimensions: int | None = None,
    timeout_seconds: float = 30.0,
) -> list[list[float]]:
    """Call an OpenAI-compatible embeddings endpoint.

    DashScope 百炼兼容接口示例：
    POST https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings
    """
    cleaned = [t for t in texts if t and t.strip()]
    if not cleaned:
        return []

    key = (api_key or os.environ.get("DASHSCOPE_API_KEY") or "").strip()
    if not key:
        raise EmbeddingError("未配置 embedding api_key，也未设置 DASHSCOPE_API_KEY")

    url = f"{_clean_base_url(base_url)}/embeddings"
    payload: dict[str, Any] = {
        "model": model,
        "input": cleaned,
        "encoding_format": "float",
    }
    if dimensions and dimensions > 0:
        payload["dimensions"] = int(dimensions)

    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            resp = client.post(
                url,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:  # noqa: BLE001
        raise EmbeddingError(f"embedding 请求失败: {e}") from e

    rows = data.get("data")
    if not isinstance(rows, list):
        raise EmbeddingError("embedding 响应缺少 data 数组")

    vectors: list[list[float]] = []
    rows_sorted = sorted(
        rows,
        key=lambda x: int(x.get("index", 0)) if isinstance(x, dict) else 0,
    )
    for row in rows_sorted:
        emb = row.get("embedding") if isinstance(row, dict) else None
        if not isinstance(emb, list):
            raise EmbeddingError("embedding 响应项缺少 embedding 数组")
        vectors.append([float(x) for x in emb])
    return vectors


__all__ = ["EmbeddingError", "embed_texts_openai_compatible"]
