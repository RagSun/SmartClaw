"""租户治理运行时状态存储（可插拔后端）。

Store 只保存"强制限额所需的可变运行时计数"：

- 请求限流的令牌桶状态（每租户）
- 当日 token 消耗量（每租户，用于配额）
- 在途请求数（每租户，用于并发上限）

提供两种后端：

- :class:`InMemoryStore`：进程私有，仅在**单进程部署**下正确。
- :class:`RedisStore`：把上述计数下沉到 Redis，使配额/限流/并发在
  **多 worker / 多副本**间共享同一份真相（解决"配额被放大 N 倍"）。所有
  读改写均通过 Redis 原子原语或 Lua 脚本完成，保证并发正确性。

两者都实现同一个 :class:`GovernanceStore` 接口，由 :func:`create_store`
按配置（``governance.store``）选择，对上层 :class:`TenantGovernor` 透明。
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # 仅类型检查期导入，运行时按需惰性导入，避免硬依赖 redis
    from smartclaw.config.loader import GovernanceConfig


class GovernanceStore(ABC):
    """租户治理状态后端接口。

    实现者需保证各方法的线程安全（同一进程内多协程/多线程并发调用）。
    """

    @abstractmethod
    def consume_rate_token(
        self,
        tenant_id: str,
        capacity: float,
        refill_per_sec: float,
        now: float | None = None,
    ) -> bool:
        """令牌桶：尝试为一次请求消耗 1 个令牌。成功返回 ``True``。"""

    @abstractmethod
    def incr_daily_tokens(self, tenant_id: str, day: str, amount: int) -> int:
        """累加某租户在 ``day``（YYYY-MM-DD）的 token 用量，返回累加后的值。"""

    @abstractmethod
    def get_daily_tokens(self, tenant_id: str, day: str) -> int:
        """读取某租户在 ``day`` 的 token 用量；非当日记录视为 0。"""

    @abstractmethod
    def acquire_slot(self, tenant_id: str, max_concurrency: int) -> bool:
        """尝试占用一个并发槽。``max_concurrency<=0`` 视为不限并发，恒成功且不计数。"""

    @abstractmethod
    def release_slot(self, tenant_id: str) -> None:
        """释放一个并发槽（计数不会降到负数）。"""


class InMemoryStore(GovernanceStore):
    """进程内、线程安全、内存有界的治理状态后端。

    内存有界性：
    - 令牌桶 / 并发槽：每租户各 1 条记录；
    - 当日用量：每租户仅保留"当前一天"一条记录，跨天自动重置，不会随天数无限增长。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # tenant -> [tokens, last_ts]
        self._buckets: dict[str, list[float]] = {}
        # tenant -> [day, count]
        self._daily: dict[str, list] = {}
        # tenant -> in-flight count
        self._slots: dict[str, int] = {}

    def consume_rate_token(
        self,
        tenant_id: str,
        capacity: float,
        refill_per_sec: float,
        now: float | None = None,
    ) -> bool:
        ts = time.monotonic() if now is None else now
        with self._lock:
            state = self._buckets.get(tenant_id)
            if state is None:
                # 首次访问：桶满
                state = [capacity, ts]
                self._buckets[tenant_id] = state
            tokens, last = state
            elapsed = max(0.0, ts - last)
            tokens = min(capacity, tokens + elapsed * refill_per_sec)
            if tokens >= 1.0:
                tokens -= 1.0
                state[0], state[1] = tokens, ts
                return True
            state[0], state[1] = tokens, ts
            return False

    def incr_daily_tokens(self, tenant_id: str, day: str, amount: int) -> int:
        with self._lock:
            rec = self._daily.get(tenant_id)
            if rec is None or rec[0] != day:
                rec = [day, 0]
                self._daily[tenant_id] = rec
            rec[1] += int(amount)
            return rec[1]

    def get_daily_tokens(self, tenant_id: str, day: str) -> int:
        with self._lock:
            rec = self._daily.get(tenant_id)
            if rec is None or rec[0] != day:
                return 0
            return int(rec[1])

    def acquire_slot(self, tenant_id: str, max_concurrency: int) -> bool:
        if max_concurrency <= 0:
            return True
        with self._lock:
            cur = self._slots.get(tenant_id, 0)
            if cur >= max_concurrency:
                return False
            self._slots[tenant_id] = cur + 1
            return True

    def release_slot(self, tenant_id: str) -> None:
        with self._lock:
            cur = self._slots.get(tenant_id, 0)
            if cur > 0:
                self._slots[tenant_id] = cur - 1


class RedisStore(GovernanceStore):
    """基于 Redis 的共享治理状态后端（多 worker / 多副本一致）。

    键空间（``ns`` 默认 ``"fc:gov"``）：

    - 令牌桶：``{ns}:rate:{tenant}`` → hash{tokens, ts}，带 TTL 自动回收空闲桶；
    - 当日用量：``{ns}:daily:{day}`` → hash{tenant: count}，整 hash 设 2 天 TTL；
    - 并发计数：``{ns}:conc:{tenant}`` → int，带安全 TTL 防止崩溃泄漏槽位。

    并发正确性：
    - 令牌桶（读-改-写）与并发 acquire（INCR-判断-回滚）用 **Lua 脚本**原子执行；
    - 时间统一取 **Redis 服务端时间**（``TIME``），避免多副本本地时钟漂移；
    - 当日累加用 ``HINCRBY``（天然原子）。
    """

    # 令牌桶：ARGV = capacity, refill_per_sec, now(<0 表示用服务端时间), ttl
    _LUA_RATE = """
    local key = KEYS[1]
    local capacity = tonumber(ARGV[1])
    local refill = tonumber(ARGV[2])
    local now = tonumber(ARGV[3])
    local ttl = tonumber(ARGV[4])
    if now < 0 then
      local t = redis.call('TIME')
      now = tonumber(t[1]) + tonumber(t[2]) / 1000000.0
    end
    local data = redis.call('HMGET', key, 'tokens', 'ts')
    local tokens = tonumber(data[1])
    local ts = tonumber(data[2])
    if tokens == nil or ts == nil then
      tokens = capacity
      ts = now
    end
    local elapsed = now - ts
    if elapsed < 0 then elapsed = 0 end
    tokens = math.min(capacity, tokens + elapsed * refill)
    local allowed = 0
    if tokens >= 1.0 then
      tokens = tokens - 1.0
      allowed = 1
    end
    redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
    if ttl > 0 then redis.call('EXPIRE', key, ttl) end
    return allowed
    """

    # 并发 acquire：ARGV = max_concurrency, ttl
    _LUA_ACQUIRE = """
    local key = KEYS[1]
    local maxc = tonumber(ARGV[1])
    local ttl = tonumber(ARGV[2])
    local cur = redis.call('INCR', key)
    if cur > maxc then
      redis.call('DECR', key)
      return 0
    end
    if ttl > 0 then redis.call('EXPIRE', key, ttl) end
    return 1
    """

    # 并发 release：不降到负数
    _LUA_RELEASE = """
    local key = KEYS[1]
    local cur = tonumber(redis.call('GET', key) or '0')
    if cur > 0 then return redis.call('DECR', key) end
    return 0
    """

    def __init__(
        self,
        redis_url: str = "",
        *,
        client: Any = None,
        namespace: str = "fc:gov",
        daily_ttl_seconds: int = 172800,  # 2 天，确保跨天后旧记录自动回收
        concurrency_ttl_seconds: int = 3600,  # 安全 TTL：防 worker 崩溃泄漏槽位
    ) -> None:
        if client is None:
            import redis  # 惰性导入：仅 store=redis 时才需要该依赖

            client = redis.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=3,
            )
        self._r = client
        self._ns = namespace.rstrip(":")
        self._daily_ttl = max(0, daily_ttl_seconds)
        self._conc_ttl = max(0, concurrency_ttl_seconds)
        self._rate_script = client.register_script(self._LUA_RATE)
        self._acquire_script = client.register_script(self._LUA_ACQUIRE)
        self._release_script = client.register_script(self._LUA_RELEASE)

    def ping(self) -> bool:
        """连通性自检（启动期 fail-fast 用）。"""
        return bool(self._r.ping())

    def _rate_key(self, tenant_id: str) -> str:
        return f"{self._ns}:rate:{tenant_id}"

    def _daily_key(self, day: str) -> str:
        return f"{self._ns}:daily:{day}"

    def _conc_key(self, tenant_id: str) -> str:
        return f"{self._ns}:conc:{tenant_id}"

    def consume_rate_token(
        self,
        tenant_id: str,
        capacity: float,
        refill_per_sec: float,
        now: float | None = None,
    ) -> bool:
        # 桶空闲多久后可丢弃：装满整桶所需时间再加 60s 余量；至少 60s。
        ttl = 60
        if refill_per_sec > 0:
            ttl = int(capacity / refill_per_sec) + 60
        res = self._rate_script(
            keys=[self._rate_key(tenant_id)],
            args=[capacity, refill_per_sec, -1 if now is None else now, ttl],
        )
        return int(res) == 1

    def incr_daily_tokens(self, tenant_id: str, day: str, amount: int) -> int:
        key = self._daily_key(day)
        pipe = self._r.pipeline()
        pipe.hincrby(key, tenant_id, int(amount))
        if self._daily_ttl > 0:
            pipe.expire(key, self._daily_ttl)
        total = pipe.execute()[0]
        return int(total)

    def get_daily_tokens(self, tenant_id: str, day: str) -> int:
        val = self._r.hget(self._daily_key(day), tenant_id)
        return int(val) if val is not None else 0

    def acquire_slot(self, tenant_id: str, max_concurrency: int) -> bool:
        if max_concurrency <= 0:
            return True
        res = self._acquire_script(
            keys=[self._conc_key(tenant_id)],
            args=[max_concurrency, self._conc_ttl],
        )
        return int(res) == 1

    def release_slot(self, tenant_id: str) -> None:
        self._release_script(keys=[self._conc_key(tenant_id)], args=[])


_REDIS_HELP = (
    "请确认 Redis 已安装并启动，且 governance.redis_url 正确：\n"
    "  • Docker:  docker run -d --name redis -p 6379:6379 redis:7-alpine\n"
    "  • Windows: 用 Memurai / WSL 内的 redis-server\n"
    "  • Linux:   sudo apt-get install redis-server && redis-server\n"
    "  • 安装客户端依赖:  uv pip install \".[redis]\"\n"
    "  • 配置示例(config.toml): [governance] store=\"redis\" "
    'redis_url="redis://127.0.0.1:6379/0"'
)


def create_store(gov: Optional["GovernanceConfig"]) -> GovernanceStore:
    """按配置选择治理状态后端。

    - ``store=="redis"`` 且配置了 ``redis_url``：返回 :class:`RedisStore`
      （启动期立即 ``ping`` 做 fail-fast，连不上直接抛错并给出安装/启动指引，
      **绝不静默降级**成内存——否则会误以为配额跨副本生效，实则没有）。
    - 其他：返回进程内 :class:`InMemoryStore`（单进程正确）。
    """
    if gov is not None and getattr(gov, "store", "memory") == "redis":
        redis_url = getattr(gov, "redis_url", "") or ""
        if not redis_url:
            raise ValueError(
                "governance.store=redis 但未配置 governance.redis_url。\n" + _REDIS_HELP
            )
        try:
            import redis  # noqa: F401  仅用于捕获其连接异常类型
        except ImportError as exc:  # pragma: no cover - 取决于是否装了 [redis] extra
            raise RuntimeError(
                "governance.store=redis 需要 redis 客户端库，但未安装。\n" + _REDIS_HELP
            ) from exc
        try:
            store = RedisStore(redis_url)
            store.ping()
        except Exception as exc:  # 连接/认证/超时等
            raise RuntimeError(
                f"无法连接 Redis（governance.redis_url={redis_url!r}）：{exc}\n"
                + _REDIS_HELP
            ) from exc
        return store
    return InMemoryStore()


__all__ = ["GovernanceStore", "InMemoryStore", "RedisStore", "create_store"]
