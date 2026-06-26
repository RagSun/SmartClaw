"""RedisReplayGuard 真实 Redis 单元测试（无 fakeredis、无回退）。

验证 webhook 防重放下沉 Redis 后，去重在多 worker / 多副本间共享：
同一 event_id 跨「两个守卫实例」也只放行一次。
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

from smartclaw.auth.webhook_replay import RedisReplayGuard, WebhookReplayGuard
from tests._redis_real import cleanup, connect_or_skip


def test_first_seen_then_replay_within_ttl():
    client, ns = connect_or_skip()
    try:
        g = RedisReplayGuard(ttl_seconds=60, client=client, namespace=ns)
        key = f"evt-{uuid.uuid4().hex}"
        assert g.is_replay(key) is False  # 首次出现
        assert g.is_replay(key) is True   # TTL 内重放
        assert g.is_replay(key) is True
    finally:
        cleanup(client, ns)


def test_empty_key_or_zero_ttl_never_replay():
    client, ns = connect_or_skip()
    try:
        g = RedisReplayGuard(ttl_seconds=60, client=client, namespace=ns)
        assert g.is_replay("") is False
        assert g.is_replay(None) is False
        g0 = RedisReplayGuard(ttl_seconds=0, client=client, namespace=ns)
        assert g0.is_replay("x") is False
        assert g0.is_replay("x") is False
    finally:
        cleanup(client, ns)


def test_ttl_expiry_allows_again():
    client, ns = connect_or_skip()
    try:
        g = RedisReplayGuard(ttl_seconds=1, client=client, namespace=ns)
        key = f"evt-{uuid.uuid4().hex}"
        assert g.is_replay(key) is False
        assert g.is_replay(key) is True
        time.sleep(1.2)  # 等 TTL 过期
        assert g.is_replay(key) is False  # 过期后视为新事件
    finally:
        cleanup(client, ns)


def test_shared_across_two_guards_simulating_workers():
    """两个守卫实例（=两个 worker）共享同一 Redis：同一事件只放行一次。"""
    client, ns = connect_or_skip()
    try:
        import redis

        client2 = redis.Redis.from_url(
            os.environ["SMARTCLAW_TEST_REDIS_URL"], decode_responses=True
        )
        g1 = RedisReplayGuard(ttl_seconds=60, client=client, namespace=ns)
        g2 = RedisReplayGuard(ttl_seconds=60, client=client2, namespace=ns)
        key = f"evt-{uuid.uuid4().hex}"
        assert g1.is_replay(key) is False   # worker1 首见，放行
        assert g2.is_replay(key) is True    # worker2 必须识别为重放（共享 Redis）
    finally:
        cleanup(client, ns)


def test_inmemory_guard_not_shared_across_instances():
    """对照组：两个进程内守卫各记各的，复现跨进程重放放行的 bug。"""
    g1 = WebhookReplayGuard(ttl_seconds=60)
    g2 = WebhookReplayGuard(ttl_seconds=60)
    key = "evt-x"
    assert g1.is_replay(key) is False
    # 第二个进程内守卫看不到 g1 的记录 → 同一事件被再次放行（这正是 bug）
    assert g2.is_replay(key) is False
