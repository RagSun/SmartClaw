"""真实 Redis 测试夹具助手（无 fakeredis、无回退）。

本项目把 Redis 作为「硬性依赖」，因此治理 / 防重放的 Redis 用例**只连真实 Redis**：

- 通过环境变量 ``SMARTCLAW_TEST_REDIS_URL`` 指定（如 ``redis://127.0.0.1:6379/0``）；
- 未设置或连不上时 **friendly skip**，并打印如何安装 / 启动 Redis 的指引，
  而不是静默通过或改用模拟实现。

每个用例用**独立 namespace**（uuid）隔离键空间，并在结束时只清理自己的键，
绝不 FLUSH 整个库，避免污染共享 Redis。
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

ENV_VAR = "SMARTCLAW_TEST_REDIS_URL"

_INSTALL_HINT = (
    f"未进行真实 Redis 验证：环境变量 {ENV_VAR} 未设置或 Redis 不可达。\n"
    "  本项目 Redis 为硬性依赖，请先准备一台真实 Redis 再跑该用例：\n"
    "    1) 启动 Redis（任选其一）：\n"
    "       • Docker:        docker run -d --name redis -p 6379:6379 redis:7-alpine\n"
    "       • docker compose: docker compose up -d redis\n"
    "       • Windows:       Memurai / WSL 内 redis-server\n"
    "       • Linux:         sudo apt-get install redis-server && redis-server\n"
    "    2) 安装客户端依赖： uv pip install \".[redis]\"\n"
    f"    3) 设置环境变量后再跑： $env:{ENV_VAR}=\"redis://127.0.0.1:6379/0\""
)


def connect_or_skip() -> tuple[Any, str]:
    """连接真实 Redis；不可用则带安装指引 skip。

    返回 ``(client, namespace)``：``client`` 为 decode_responses 的 redis 客户端，
    ``namespace`` 为本次用例独占的键前缀。
    """
    url = os.environ.get(ENV_VAR, "").strip()
    if not url:
        pytest.skip(_INSTALL_HINT)
    try:
        import redis
    except ImportError:
        pytest.skip(
            "未安装 redis 客户端库（uv pip install \".[redis]\"）。\n" + _INSTALL_HINT
        )
    try:
        client = redis.Redis.from_url(
            url, decode_responses=True, socket_connect_timeout=3, socket_timeout=3
        )
        client.ping()
    except Exception as exc:  # 连接/认证/超时
        pytest.skip(f"Redis 不可达（{url}）：{exc}\n" + _INSTALL_HINT)
    namespace = f"fc:test:{uuid.uuid4().hex[:12]}"
    return client, namespace


def cleanup(client: Any, namespace: str) -> None:
    """删除本用例 namespace 下的全部键（不触碰其他键）。"""
    keys = list(client.scan_iter(f"{namespace}:*"))
    if keys:
        client.delete(*keys)
