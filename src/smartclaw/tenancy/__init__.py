"""租户生命周期管理（运营化注册表 + 管理 API）。

把租户从「config.toml 静态映射」升级为可 CRUD 的运行时实体，支撑开通 / 停用 /
配额管理 / app_id 路由，并与治理层（限额覆盖、停用拒绝）、租户解析打通。

公开入口：
- :class:`TenantRegistry`（SQLite 本地）/ :func:`create_tenant_registry`（按配置选后端）
- :func:`get_tenant_registry` / :func:`reset_tenant_registry`（进程级单例）
- :data:`router`（FastAPI ``/api/admin/tenants``）

后端按 ``governance.store`` 选择：``memory``→本地 SQLite（单副本正确）；
``redis``→ :class:`~smartclaw.tenancy.redis_registry.RedisTenantRegistry`（共享，多副本一致）。
"""

from smartclaw.tenancy.registry import (
    TenantExistsError,
    TenantNotFoundError,
    TenantRegistry,
    create_tenant_registry,
    get_tenant_registry,
    reset_tenant_registry,
)

__all__ = [
    "TenantExistsError",
    "TenantNotFoundError",
    "TenantRegistry",
    "create_tenant_registry",
    "get_tenant_registry",
    "reset_tenant_registry",
]
