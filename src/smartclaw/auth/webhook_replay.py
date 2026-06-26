"""Webhook 短期防重放。

两种后端，接口一致（``is_replay(key) -> bool``）：

- :class:`WebhookReplayGuard`：进程内字典，仅单进程正确（多 worker 会各记各的，
  导致同一事件在不同 worker 被各放行一次）。
- :class:`RedisReplayGuard`：用 Redis ``SET key 1 NX EX ttl`` 原子地"首见即占位"，
  使去重在多 worker / 多副本间共享，彻底修复跨进程重放。

由 :func:`get_replay_guard` 按是否提供 ``redis_url`` 选择后端并做单例缓存。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional


class WebhookReplayGuard:
    """进程内防重放（仅单进程正确）。"""

    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = max(0, ttl_seconds)
        self._lock = threading.Lock()
        self._seen: dict[str, float] = {}

    def is_replay(self, key: Optional[str]) -> bool:
        if self.ttl_seconds <= 0 or not key:
            return False
        now = time.time()
        with self._lock:
            self._prune(now)
            if key in self._seen:
                return True
            self._seen[key] = now
            return False

    def _prune(self, now: float) -> None:
        cutoff = now - self.ttl_seconds
        dead = [k for k, t in self._seen.items() if t < cutoff]
        for k in dead:
            del self._seen[k]


class RedisReplayGuard:
    """基于 Redis 的跨进程防重放。

    ``SET {ns}:{key} 1 NX EX ttl``：返回真表示首次出现（非重放），返回空表示
    已存在（重放）。TTL 由 Redis 负责过期回收，无需手工 prune。
    """

    def __init__(
        self,
        ttl_seconds: int,
        redis_url: str = "",
        *,
        client: Any = None,
        namespace: str = "fc:replay",
    ) -> None:
        self.ttl_seconds = max(0, ttl_seconds)
        self._ns = namespace.rstrip(":")
        if client is None:
            import redis  # 惰性导入

            client = redis.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=3,
            )
        self._r = client

    def is_replay(self, key: Optional[str]) -> bool:
        if self.ttl_seconds <= 0 or not key:
            return False
        ok = self._r.set(f"{self._ns}:{key}", "1", nx=True, ex=self.ttl_seconds)
        # set(nx=True) 首次成功返回 True；键已存在返回 None → 视为重放。
        return not ok


_guard: Optional[Any] = None
_guard_sig: tuple[int, str] = (-1, "")
_guard_lock = threading.Lock()


def get_replay_guard(ttl_seconds: int, redis_url: str = "") -> Any:
    """获取防重放守卫单例。

    ``redis_url`` 非空 → :class:`RedisReplayGuard`（跨进程），否则进程内守卫。
    缓存键含 ``(ttl, redis_url)``，配置变化会自动重建。
    """
    global _guard, _guard_sig
    sig = (int(ttl_seconds), redis_url or "")
    with _guard_lock:
        if _guard is None or _guard_sig != sig:
            if redis_url:
                _guard = RedisReplayGuard(ttl_seconds, redis_url)
            else:
                _guard = WebhookReplayGuard(ttl_seconds)
            _guard_sig = sig
        return _guard
