"""RedisStore 真实 Redis 单元测试（无 fakeredis、无回退）。

需要真实 Redis：设置 SMARTCLAW_TEST_REDIS_URL 后运行；未设置则带安装指引 skip。
每个用例独立 namespace 隔离，结束清理自身键。
"""

from __future__ import annotations

import pytest

from smartclaw.config.loader import GovernanceConfig
from smartclaw.governance.store import (
    InMemoryStore,
    RedisStore,
    create_store,
)
from tests._redis_real import cleanup, connect_or_skip


@pytest.fixture()
def store():
    client, ns = connect_or_skip()
    yield RedisStore(client=client, namespace=ns)
    cleanup(client, ns)


# ----- 令牌桶限流 -----

def test_token_bucket_consumes_until_empty_then_refills(store):
    t = 1000.0
    assert store.consume_rate_token("t1", capacity=2, refill_per_sec=1.0, now=t) is True
    assert store.consume_rate_token("t1", capacity=2, refill_per_sec=1.0, now=t) is True
    assert store.consume_rate_token("t1", capacity=2, refill_per_sec=1.0, now=t) is False
    # 1 秒补 1 个
    assert store.consume_rate_token("t1", capacity=2, refill_per_sec=1.0, now=t + 1.0) is True
    assert store.consume_rate_token("t1", capacity=2, refill_per_sec=1.0, now=t + 1.0) is False


def test_token_bucket_is_per_tenant(store):
    assert store.consume_rate_token("a", capacity=1, refill_per_sec=0.0, now=0.0) is True
    assert store.consume_rate_token("a", capacity=1, refill_per_sec=0.0, now=0.0) is False
    # 另一租户独立计量
    assert store.consume_rate_token("b", capacity=1, refill_per_sec=0.0, now=0.0) is True


def test_token_bucket_server_time_path(store):
    # now=None 走 Redis 服务端 TIME；capacity=2,refill=0 → 仅 2 个放行
    res = [store.consume_rate_token("srv", capacity=2, refill_per_sec=0.0) for _ in range(3)]
    assert res == [True, True, False]


# ----- 当日 token 配额累计 -----

def test_daily_tokens_accumulate_and_isolate(store):
    assert store.incr_daily_tokens("t1", "2026-06-09", 100) == 100
    assert store.incr_daily_tokens("t1", "2026-06-09", 50) == 150
    assert store.get_daily_tokens("t1", "2026-06-09") == 150
    # 不同日期独立
    assert store.get_daily_tokens("t1", "2026-06-08") == 0
    # 不同租户独立
    assert store.get_daily_tokens("t2", "2026-06-09") == 0


# ----- 并发槽 -----

def test_concurrency_slots_acquire_release(store):
    assert store.acquire_slot("t1", 2) is True
    assert store.acquire_slot("t1", 2) is True
    assert store.acquire_slot("t1", 2) is False  # 已满
    store.release_slot("t1")
    assert store.acquire_slot("t1", 2) is True  # 释放后可再取


def test_concurrency_unlimited_when_non_positive(store):
    # max<=0 视为不限，恒成功且不计数
    for _ in range(20):
        assert store.acquire_slot("t1", 0) is True


def test_release_never_goes_negative(store):
    store.release_slot("t1")
    store.release_slot("t1")
    assert store.acquire_slot("t1", 1) is True
    assert store.acquire_slot("t1", 1) is False


# ----- 跨实例（模拟多 worker / 多副本）共享同一份真相 -----

def test_two_stores_share_quota_and_concurrency():
    """两个独立 RedisStore 实例（=两个 worker）必须读到同一份计数。

    这是 P0-2 的核心：把状态下沉到 Redis 后，配额/并发在副本间共享，
    不再「每进程各算各的」导致放大 N 倍。
    """
    client, ns = connect_or_skip()
    try:
        w1 = RedisStore(client=client, namespace=ns)
        # 第二个 worker 用「另建的客户端」连同一 Redis、同一 namespace
        import redis
        import os

        client2 = redis.Redis.from_url(
            os.environ["SMARTCLAW_TEST_REDIS_URL"], decode_responses=True
        )
        w2 = RedisStore(client=client2, namespace=ns)

        # 配额：w1 记 80，w2 应立刻看到 80
        w1.incr_daily_tokens("acme", "2026-06-09", 80)
        assert w2.get_daily_tokens("acme", "2026-06-09") == 80
        # w2 再记 80 → 共享累计 160
        assert w2.incr_daily_tokens("acme", "2026-06-09", 80) == 160
        assert w1.get_daily_tokens("acme", "2026-06-09") == 160

        # 并发上限 2：w1 占 1、w2 占 1 后，任一 worker 再占都应失败
        assert w1.acquire_slot("acme", 2) is True
        assert w2.acquire_slot("acme", 2) is True
        assert w1.acquire_slot("acme", 2) is False
        assert w2.acquire_slot("acme", 2) is False
    finally:
        cleanup(client, ns)


def test_inmemory_does_not_share_across_instances():
    """对照组：两个 InMemoryStore（=两个进程内存）各算各的，复现被放大的 bug。"""
    a = InMemoryStore()
    b = InMemoryStore()
    a.incr_daily_tokens("acme", "2026-06-09", 80)
    # b 看不到 a 的累计 → 这正是单进程内存在多副本下失效的根因
    assert b.get_daily_tokens("acme", "2026-06-09") == 0


# ----- 用户级复合主体键：在 Redis 上同样与纯租户键隔离（零债务关键保证） -----

def test_user_subject_key_isolated_on_redis(store):
    """用户复合键 ``tenant|u:open_id`` 复用同一 Store，与纯租户键互不串。"""
    from smartclaw.governance.governor import tenant_subject, user_subject

    day = "2026-06-12"
    store.incr_daily_tokens(tenant_subject("acme"), day, 100)
    store.incr_daily_tokens(user_subject("acme", "ou_a"), day, 7)
    store.incr_daily_tokens(user_subject("acme", "ou_b"), day, 3)
    # 三者各自独立
    assert store.get_daily_tokens(tenant_subject("acme"), day) == 100
    assert store.get_daily_tokens(user_subject("acme", "ou_a"), day) == 7
    assert store.get_daily_tokens(user_subject("acme", "ou_b"), day) == 3
    # 用户级并发槽同样独立
    assert store.acquire_slot(user_subject("acme", "ou_a"), 1) is True
    assert store.acquire_slot(user_subject("acme", "ou_a"), 1) is False
    assert store.acquire_slot(user_subject("acme", "ou_b"), 1) is True


# ----- create_store 工厂：硬性 Redis、无回退、fail-fast -----

def test_create_store_redis_ok():
    client, ns = connect_or_skip()
    try:
        import os

        gov = GovernanceConfig(store="redis", redis_url=os.environ["SMARTCLAW_TEST_REDIS_URL"])
        s = create_store(gov)
        assert isinstance(s, RedisStore)
        assert s.ping() is True
    finally:
        cleanup(client, ns)


def test_create_store_redis_without_url_raises():
    gov = GovernanceConfig(store="redis", redis_url="")
    with pytest.raises(ValueError):
        create_store(gov)


def test_create_store_redis_unreachable_fail_fast():
    # 指向一个不可达端口：必须 fail-fast 抛错，而不是回退到内存
    gov = GovernanceConfig(store="redis", redis_url="redis://127.0.0.1:6390/0")
    with pytest.raises(RuntimeError):
        create_store(gov)


def test_create_store_memory_default():
    gov = GovernanceConfig(store="memory", redis_url="")
    assert isinstance(create_store(gov), InMemoryStore)
