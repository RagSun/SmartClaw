"""InMemoryStore 单元测试（纯内存，无网络，不依赖 pytest-asyncio）。"""

from __future__ import annotations

from smartclaw.governance.store import InMemoryStore


def test_token_bucket_consumes_until_empty_then_refills():
    store = InMemoryStore()
    # 容量 2，每秒补充 1 个；用受控的 now 驱动时间，避免依赖 wall clock。
    t = 1000.0
    assert store.consume_rate_token("t1", capacity=2, refill_per_sec=1.0, now=t) is True
    assert store.consume_rate_token("t1", capacity=2, refill_per_sec=1.0, now=t) is True
    # 桶已空
    assert store.consume_rate_token("t1", capacity=2, refill_per_sec=1.0, now=t) is False
    # 1 秒后补充 1 个令牌
    assert store.consume_rate_token("t1", capacity=2, refill_per_sec=1.0, now=t + 1.0) is True
    assert store.consume_rate_token("t1", capacity=2, refill_per_sec=1.0, now=t + 1.0) is False


def test_token_bucket_is_per_tenant():
    store = InMemoryStore()
    t = 0.0
    assert store.consume_rate_token("a", capacity=1, refill_per_sec=0.0, now=t) is True
    assert store.consume_rate_token("a", capacity=1, refill_per_sec=0.0, now=t) is False
    # 另一租户互不影响
    assert store.consume_rate_token("b", capacity=1, refill_per_sec=0.0, now=t) is True


def test_daily_tokens_accumulate_and_reset_on_new_day():
    store = InMemoryStore()
    assert store.get_daily_tokens("t1", "2026-06-09") == 0
    assert store.incr_daily_tokens("t1", "2026-06-09", 30) == 30
    assert store.incr_daily_tokens("t1", "2026-06-09", 20) == 50
    assert store.get_daily_tokens("t1", "2026-06-09") == 50
    # 跨天自动重置（内存有界）
    assert store.get_daily_tokens("t1", "2026-06-10") == 0
    assert store.incr_daily_tokens("t1", "2026-06-10", 5) == 5


def test_concurrency_slots_acquire_and_release():
    store = InMemoryStore()
    assert store.acquire_slot("t1", max_concurrency=2) is True
    assert store.acquire_slot("t1", max_concurrency=2) is True
    assert store.acquire_slot("t1", max_concurrency=2) is False
    store.release_slot("t1")
    assert store.acquire_slot("t1", max_concurrency=2) is True


def test_concurrency_unlimited_when_max_non_positive():
    store = InMemoryStore()
    for _ in range(100):
        assert store.acquire_slot("t1", max_concurrency=0) is True


def test_release_never_goes_negative():
    store = InMemoryStore()
    store.release_slot("t1")  # 未占用即释放，不应报错或变负
    assert store.acquire_slot("t1", max_concurrency=1) is True
