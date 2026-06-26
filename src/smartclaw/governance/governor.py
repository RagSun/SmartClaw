"""租户治理器（TenantGovernor）。

在请求进入 Agent 主流程前做"准入控制"：

1. **每日 token 配额**：当日用量达上限即拒绝（``quota_exceeded``）。
2. **请求限流**：令牌桶按"每分钟请求数"限速（``rate_limited``）。
3. **并发上限**：每租户在途请求数上限（``concurrency_limited``）。

设计要点：
- ``governance.enabled=False`` 时所有方法直接放行，对既有行为**零影响**。
- 限额来自配置：全局默认 + 每租户覆盖（``None`` 继承默认，``0`` 表示不限）。
- 配额计数由 :meth:`record_tokens` 累计，数据源是真实 LLM 用量（见 metrics 集成）。
- 通过注入 ``config_provider`` 与 ``store``，本类**无全局状态**，便于单元测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Optional

from smartclaw.governance.store import GovernanceStore, InMemoryStore

if TYPE_CHECKING:
    from smartclaw.config.loader import Config, GovernanceConfig


@dataclass(frozen=True)
class TenantLimits:
    """某租户最终生效的限额（0 表示该维度不限）。"""

    rate_per_min: int = 0
    burst: int = 0
    daily_token_quota: int = 0
    max_concurrency: int = 0


@dataclass(frozen=True)
class Admission:
    """准入决策结果。"""

    allowed: bool
    reason: str = ""  # "" | rate_limited | quota_exceeded | concurrency_limited
    user_message: str = ""
    retry_after: float = 0.0


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---- 治理状态的「主体键」（单一真相源，杜绝多处拼接漂移）----
# 关键设计：Store 把第一个参数当作不透明字符串键，因此「用户级」无需新增任何
# Store 方法 / Redis Lua，只是换一个主体键再跑现有逻辑。tenant_id 已被规范化为
# ``[A-Za-z0-9_.-]``，故 ``|`` 与 ``:`` 是绝不与纯租户键冲突的安全分隔符。
def tenant_subject(tenant_id: str) -> str:
    """纯租户主体键（与历史行为完全一致）。"""
    return tenant_id


def user_subject(tenant_id: str, open_id: str) -> str:
    """租户内某用户的主体键；仅用户级使用，绝不与纯租户键碰撞。"""
    return f"{tenant_id}|u:{open_id}"


class TenantGovernor:
    """租户级限流 / 配额 / 并发的统一准入控制器。"""

    def __init__(
        self,
        store: Optional[GovernanceStore] = None,
        config_provider: Optional[Callable[[], "Config"]] = None,
        registry: Optional[Any] = None,
    ) -> None:
        self._store: GovernanceStore = store or InMemoryStore()
        if config_provider is None:
            from smartclaw.config.loader import get_config

            config_provider = get_config
        self._config_provider = config_provider
        # 可选：租户注册表（运营化真相源）。提供时其「停用状态」与「限额覆盖」
        # 优先于 config.toml；为 None 时退回纯配置行为（不触碰注册表 DB）。
        self._registry = registry

    # ----- 配置解析 -----

    def _governance(self) -> Optional["GovernanceConfig"]:
        gov = getattr(self._config_provider(), "governance", None)
        if gov is None or not getattr(gov, "enabled", False):
            return None
        return gov

    def _limits_for(self, gov: "GovernanceConfig", tenant_id: str) -> TenantLimits:
        rate = gov.default_rate_per_min
        burst = gov.default_burst
        quota = gov.default_daily_token_quota
        conc = gov.default_max_concurrency
        ov = (gov.per_tenant or {}).get(tenant_id)
        if ov is not None:
            if ov.rate_per_min is not None:
                rate = ov.rate_per_min
            if ov.burst is not None:
                burst = ov.burst
            if ov.daily_token_quota is not None:
                quota = ov.daily_token_quota
            if ov.max_concurrency is not None:
                conc = ov.max_concurrency
        # 租户注册表的限额覆盖优先于 config.toml（运营化动态调整，无需改配置重启）。
        reg_limits = self._registry_limits(tenant_id)
        if reg_limits is not None:
            if reg_limits.get("rate_per_min") is not None:
                rate = reg_limits["rate_per_min"]
            if reg_limits.get("burst") is not None:
                burst = reg_limits["burst"]
            if reg_limits.get("daily_token_quota") is not None:
                quota = reg_limits["daily_token_quota"]
            if reg_limits.get("max_concurrency") is not None:
                conc = reg_limits["max_concurrency"]
        # 令牌桶容量：未显式配置 burst 时，用"每分钟速率"作为容量（允许一分钟的突发）。
        if burst <= 0 and rate > 0:
            burst = rate
        return TenantLimits(
            rate_per_min=max(0, rate),
            burst=max(0, burst),
            daily_token_quota=max(0, quota),
            max_concurrency=max(0, conc),
        )

    # ----- 准入：限流 + 配额（无持有资源，可在入口直接判定） -----

    def _registry_limits(self, tenant_id: str) -> Optional[dict]:
        if self._registry is None:
            return None
        try:
            return self._registry.effective_limits(tenant_id)
        except Exception:
            return None

    def _is_suspended(self, tenant_id: str) -> bool:
        if self._registry is None:
            return False
        try:
            return bool(self._registry.is_suspended(tenant_id))
        except Exception:
            return False

    def admit(self, tenant_id: str) -> Admission:
        # 0) 租户停用：运营层硬开关，优先于一切，且不受 governance.enabled 影响。
        if self._is_suspended(tenant_id):
            return Admission(
                False,
                reason="tenant_suspended",
                user_message="⛔ 该租户已停用，请联系管理员开通后再使用。",
            )

        gov = self._governance()
        if gov is None:
            return Admission(True)
        limits = self._limits_for(gov, tenant_id)

        # 1) 配额（先判，避免被限流"挡在前面"而看不到真实拒因）
        if limits.daily_token_quota > 0:
            used = self._store.get_daily_tokens(tenant_id, _today_utc())
            if used >= limits.daily_token_quota:
                return Admission(
                    False,
                    reason="quota_exceeded",
                    user_message=(
                        "⛔ 今日额度已用尽（租户级 token 配额），请明日再试或联系管理员提升额度。"
                    ),
                )

        # 2) 限流（令牌桶）
        if limits.rate_per_min > 0:
            refill = limits.rate_per_min / 60.0
            ok = self._store.consume_rate_token(
                tenant_id, capacity=float(limits.burst or limits.rate_per_min), refill_per_sec=refill
            )
            if not ok:
                return Admission(
                    False,
                    reason="rate_limited",
                    user_message="⏳ 当前请求过于频繁（租户级限流），请稍后再试。",
                    retry_after=1.0 / refill if refill > 0 else 1.0,
                )

        return Admission(True)

    # ----- 并发：需在 try/finally 中配对 acquire / release -----

    def acquire(self, tenant_id: str) -> Admission:
        gov = self._governance()
        if gov is None:
            return Admission(True)
        limits = self._limits_for(gov, tenant_id)
        if limits.max_concurrency <= 0:
            return Admission(True)
        if self._store.acquire_slot(tenant_id, limits.max_concurrency):
            return Admission(True)
        return Admission(
            False,
            reason="concurrency_limited",
            user_message="🚦 当前并发请求过多（租户级并发上限），请稍后再试。",
        )

    def release(self, tenant_id: str) -> None:
        # 释放是幂等且无害的；即便治理被关闭也安全（计数本就为 0）。
        self._store.release_slot(tenant_id)

    # ----- 用量累计与快照 -----

    def record_tokens(self, tenant_id: str, total_tokens: int) -> None:
        if self._governance() is None or total_tokens <= 0:
            return
        self._store.incr_daily_tokens(tenant_id, _today_utc(), int(total_tokens))

    def snapshot(self, tenant_id: str) -> dict:
        gov = self._governance()
        if gov is None:
            return {"enabled": False, "tenant_id": tenant_id}
        limits = self._limits_for(gov, tenant_id)
        return {
            "enabled": True,
            "tenant_id": tenant_id,
            "limits": {
                "rate_per_min": limits.rate_per_min,
                "burst": limits.burst,
                "daily_token_quota": limits.daily_token_quota,
                "max_concurrency": limits.max_concurrency,
            },
            "used_today": self._store.get_daily_tokens(tenant_id, _today_utc()),
            "day": _today_utc(),
        }

    # ===================================================================== #
    # 用户级配额（纯增量）：与上面的租户级方法**并行**，互不影响。
    # 复用同一套 Store 方法 + 「用户主体键」，故无新增 Store 抽象/Lua。
    # 未配置用户限额（默认全 0/空）或无 open_id 时，所有 *_user 方法立即放行。
    # ===================================================================== #

    def _user_limits_for(
        self, gov: "GovernanceConfig", tenant_id: str, open_id: str
    ) -> TenantLimits:
        rate = getattr(gov, "default_user_rate_per_min", 0)
        quota = getattr(gov, "default_user_daily_token_quota", 0)
        conc = getattr(gov, "default_user_max_concurrency", 0)
        ov = (getattr(gov, "per_user_by_tenant", None) or {}).get(tenant_id, {}).get(open_id)
        if ov is not None:
            if ov.rate_per_min is not None:
                rate = ov.rate_per_min
            if ov.daily_token_quota is not None:
                quota = ov.daily_token_quota
            if ov.max_concurrency is not None:
                conc = ov.max_concurrency
        # 用户级突发额度直接取速率（不引入新配置项，减少长期维护面）。
        burst = rate if rate > 0 else 0
        return TenantLimits(
            rate_per_min=max(0, rate),
            burst=max(0, burst),
            daily_token_quota=max(0, quota),
            max_concurrency=max(0, conc),
        )

    def admit_user(self, tenant_id: str, open_id: str) -> Admission:
        """用户级准入：仅校验「用户配额 + 用户限流」。

        租户级（停用/配额/限流）已由 :meth:`admit` 在更前面判定；本方法只在其
        放行之后追加「同一租户内单用户」的公平性约束。
        """
        gov = self._governance()
        if gov is None or not open_id:
            return Admission(True)
        limits = self._user_limits_for(gov, tenant_id, open_id)
        key = user_subject(tenant_id, open_id)

        # 1) 用户每日 token 配额
        if limits.daily_token_quota > 0:
            used = self._store.get_daily_tokens(key, _today_utc())
            if used >= limits.daily_token_quota:
                return Admission(
                    False,
                    reason="user_quota_exceeded",
                    user_message=(
                        "⛔ 您今日的个人额度已用尽，请明日再试或联系管理员提升额度。"
                    ),
                )

        # 2) 用户限流（令牌桶）
        if limits.rate_per_min > 0:
            refill = limits.rate_per_min / 60.0
            ok = self._store.consume_rate_token(
                key,
                capacity=float(limits.burst or limits.rate_per_min),
                refill_per_sec=refill,
            )
            if not ok:
                return Admission(
                    False,
                    reason="user_rate_limited",
                    user_message="⏳ 您的操作过于频繁（个人限流），请稍后再试。",
                    retry_after=1.0 / refill if refill > 0 else 1.0,
                )

        return Admission(True)

    def acquire_user(self, tenant_id: str, open_id: str) -> Admission:
        gov = self._governance()
        if gov is None or not open_id:
            return Admission(True)
        limits = self._user_limits_for(gov, tenant_id, open_id)
        if limits.max_concurrency <= 0:
            return Admission(True)
        if self._store.acquire_slot(
            user_subject(tenant_id, open_id), limits.max_concurrency
        ):
            return Admission(True)
        return Admission(
            False,
            reason="user_concurrency_limited",
            user_message="🚦 您当前的并发任务过多（个人并发上限），请稍后再试。",
        )

    def release_user(self, tenant_id: str, open_id: str) -> None:
        # 释放幂等无害；无 open_id 时无操作。
        if open_id:
            self._store.release_slot(user_subject(tenant_id, open_id))

    def record_user_tokens(self, tenant_id: str, open_id: str, total_tokens: int) -> None:
        if self._governance() is None or not open_id or total_tokens <= 0:
            return
        self._store.incr_daily_tokens(
            user_subject(tenant_id, open_id), _today_utc(), int(total_tokens)
        )

    def user_snapshot(self, tenant_id: str, open_id: str) -> dict:
        gov = self._governance()
        if gov is None:
            return {"enabled": False, "tenant_id": tenant_id, "open_id": open_id}
        limits = self._user_limits_for(gov, tenant_id, open_id)
        return {
            "enabled": True,
            "tenant_id": tenant_id,
            "open_id": open_id,
            "limits": {
                "rate_per_min": limits.rate_per_min,
                "daily_token_quota": limits.daily_token_quota,
                "max_concurrency": limits.max_concurrency,
            },
            "used_today": self._store.get_daily_tokens(
                user_subject(tenant_id, open_id), _today_utc()
            ),
            "day": _today_utc(),
        }


# ----- 进程级单例（生产使用） -----

_governor: Optional[TenantGovernor] = None


def get_governor() -> TenantGovernor:
    """获取进程级 TenantGovernor 单例。

    按 ``governance.store`` 选择后端：``memory`` → 进程内；``redis`` → 共享
    Redis（多 worker / 多副本一致）。后端在首次取单例时构建，连不上 Redis 会
    在此 fail-fast，避免静默降级成"每进程各算各的"。
    """
    global _governor
    if _governor is None:
        from smartclaw.config.loader import get_config
        from smartclaw.governance.store import create_store

        gov = getattr(get_config(), "governance", None)
        registry = None
        try:
            from smartclaw.tenancy.registry import get_tenant_registry

            registry = get_tenant_registry()
        except Exception:
            registry = None
        _governor = TenantGovernor(store=create_store(gov), registry=registry)
    return _governor


def reset_governor() -> None:
    """重置单例（仅供测试 / 配置热重载后调用）。"""
    global _governor
    _governor = None


__all__ = [
    "Admission",
    "TenantGovernor",
    "TenantLimits",
    "get_governor",
    "reset_governor",
]
