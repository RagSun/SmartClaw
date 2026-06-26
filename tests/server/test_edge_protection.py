"""HTTP 边缘防护（P0-C）：请求体大小上限。

复用**生产中间件函数** ``server._limit_request_body`` 挂到一个轻量 app（不触发
重型 lifespan），验证：

- 声明超大 Content-Length → 413（在路由处理前拦截）；
- 正常请求 → 放行；
- ``max_request_bytes=0`` → 不限制；
- ``_body_too_large`` 兜底（无 Content-Length 的 chunked 场景）。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from smartclaw import server
from smartclaw.config.loader import Config


def _make_client(monkeypatch, max_bytes: int) -> TestClient:
    cfg = Config()
    cfg.server.max_request_bytes = max_bytes
    monkeypatch.setattr(server, "get_config", lambda: cfg)

    app = FastAPI()
    # 挂上生产中间件函数本体（确保测的是真实代码路径）
    app.middleware("http")(server._limit_request_body)

    @app.post("/echo")
    async def _echo(payload: dict):
        return {"ok": True, "n": len(payload)}

    return TestClient(app)


def test_oversized_content_length_rejected(monkeypatch):
    client = _make_client(monkeypatch, max_bytes=100)
    big = {"data": "x" * 500}
    r = client.post("/echo", json=big)
    assert r.status_code == 413


def test_normal_request_passes(monkeypatch):
    client = _make_client(monkeypatch, max_bytes=100000)
    r = client.post("/echo", json={"data": "hello"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_zero_means_unlimited(monkeypatch):
    client = _make_client(monkeypatch, max_bytes=0)
    r = client.post("/echo", json={"data": "y" * 10000})
    assert r.status_code == 200


def test_invalid_content_length_rejected(monkeypatch):
    client = _make_client(monkeypatch, max_bytes=100)
    r = client.post(
        "/echo",
        content=b"{}",
        headers={"Content-Length": "not-a-number", "Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_body_too_large_helper(monkeypatch):
    cfg = Config()
    cfg.server.max_request_bytes = 10
    monkeypatch.setattr(server, "get_config", lambda: cfg)
    assert server._body_too_large(b"x" * 11) is True
    assert server._body_too_large(b"x" * 10) is False

    cfg.server.max_request_bytes = 0  # 不限制
    assert server._body_too_large(b"x" * 99999) is False
