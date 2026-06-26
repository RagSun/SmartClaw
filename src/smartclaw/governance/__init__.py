"""租户资源治理（限流 / 配额 / 并发）。

工业级多租户的 P0 能力：在请求进入 Agent 主流程前做按租户的准入控制，
防止单租户打满 LLM 配额与宿主资源（noisy neighbor / DoS）。

公开入口：
- :func:`get_governor` / :func:`reset_governor`
- :class:`TenantGovernor` / :class:`Admission`
- :class:`GovernanceStore` / :class:`InMemoryStore` / :class:`RedisStore` / :func:`create_store`
"""

from smartclaw.governance.governor import (
    Admission,
    TenantGovernor,
    TenantLimits,
    get_governor,
    reset_governor,
    tenant_subject,
    user_subject,
)
from smartclaw.governance.store import (
    GovernanceStore,
    InMemoryStore,
    RedisStore,
    create_store,
)

__all__ = [
    "Admission",
    "GovernanceStore",
    "InMemoryStore",
    "RedisStore",
    "TenantGovernor",
    "TenantLimits",
    "create_store",
    "get_governor",
    "reset_governor",
    "tenant_subject",
    "user_subject",
]
