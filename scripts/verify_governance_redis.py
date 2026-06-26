# -*- coding: utf-8 -*-
"""治理后端真实演示（命令行可复现）：多 worker 共享 vs 进程内不共享 + 限流/并发/fail-fast。

目标：用真实 Redis 证明治理状态在多 worker / 多副本间共享同一份真相，从而杜绝
"配额被放大 N 倍 / 限流形同虚设"；并证明 ``create_store`` 在 Redis 不可达时
**fail-fast、绝不静默降级**。

前置：真实 Redis 在 127.0.0.1:6379（可用 ``docker compose up -d redis``）。
用法：
    $env:PYTHONPATH="src"
    python scripts/verify_governance_redis.py
"""

from __future__ import annotations

import datetime
import types

from smartclaw.governance.store import InMemoryStore, RedisStore, create_store

URL = "redis://127.0.0.1:6379/0"
NS = "fc:demo"
DAY = datetime.date.today().isoformat()
TENANT = "dept_a"


def _cleanup(client) -> None:
    for k in client.scan_iter(f"{NS}:*"):
        client.delete(k)


def main() -> None:
    import redis

    client = redis.Redis.from_url(URL, decode_responses=True)
    _cleanup(client)

    print("==================== 1) 跨 worker 共享日配额（Redis） ====================")
    worker_a = RedisStore(URL, namespace=NS)
    worker_b = RedisStore(URL, namespace=NS)
    print(f"workerA 累加 3000 tokens -> 累计 = {worker_a.incr_daily_tokens(TENANT, DAY, 3000)}")
    print(f"workerB 累加 2500 tokens -> 累计 = {worker_b.incr_daily_tokens(TENANT, DAY, 2500)}")
    print(f"workerB 读取当日用量    -> {worker_b.get_daily_tokens(TENANT, DAY)}  （两 worker 看到同一真相）")

    print("\n==================== 2) 对比：进程内 InMemory 不共享 ====================")
    mem_a, mem_b = InMemoryStore(), InMemoryStore()
    mem_a.incr_daily_tokens(TENANT, DAY, 3000)
    mem_b.incr_daily_tokens(TENANT, DAY, 2500)
    print(f"InMemory workerA 视角 = {mem_a.get_daily_tokens(TENANT, DAY)}")
    print(f"InMemory workerB 视角 = {mem_b.get_daily_tokens(TENANT, DAY)}  -> 各算各的，配额被放大 N 倍（这正是硬伤）")

    print("\n==================== 3) 令牌桶限流（capacity=3, refill=0） ====================")
    for i in range(1, 6):
        ok = worker_a.consume_rate_token(TENANT + ":rl", capacity=3, refill_per_sec=0, now=1000.0)
        print(f"第 {i} 次请求 -> {'放行 PASS' if ok else '限流 DENY'}")

    print("\n==================== 4) 并发槽 max_concurrency=2 ====================")
    print("acquire #1 ->", worker_a.acquire_slot(TENANT + ":c", 2))
    print("acquire #2 ->", worker_a.acquire_slot(TENANT + ":c", 2))
    print("acquire #3 ->", worker_a.acquire_slot(TENANT + ":c", 2), " （超并发，拒绝）")
    worker_a.release_slot(TENANT + ":c")
    print("release 1 后 acquire #4 ->", worker_a.acquire_slot(TENANT + ":c", 2), " （释放后又可进）")

    print("\n==================== 5) no-fallback：连不上 Redis 直接 fail-fast ====================")
    gov = types.SimpleNamespace(store="redis", redis_url="redis://127.0.0.1:6399/0")  # 错误端口
    try:
        create_store(gov)
        print("不应到达这里")
    except RuntimeError as exc:
        print("create_store 抛出 RuntimeError（绝不静默降级内存）：")
        print("  " + str(exc).splitlines()[0])

    _cleanup(client)
    print("\n[governance redis OK]")


if __name__ == "__main__":
    main()
