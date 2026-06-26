"""租户注册表（运营化真相源，SQLite 持久化）。

把租户从「config.toml 静态映射」升级为「可增删改查的运行时实体」，支撑开通 /
停用 / 配额管理 / app_id 路由，无需改配置重启。

一张表 ``tenants``：

- 身份：``tenant_id``(主键，规范化) / ``display_name``
- 生命周期：``status`` = ``active`` | ``suspended``
- 治理限额覆盖（NULL=继承全局默认，0=该维度不限）：
  ``rate_per_min`` / ``burst`` / ``daily_token_quota`` / ``max_concurrency``
- 路由：``app_ids``（JSON 数组，飞书/企微 app_id → 该租户）
- 备注：``metadata``（JSON 对象，自由扩展，如计费/联系人）
- 审计：``created_at`` / ``updated_at``

线程安全：单连接 + 互斥锁（FastAPI 在线程池中调用同步处理函数）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

from smartclaw.tenant import normalize_tenant_id

if TYPE_CHECKING:
    from smartclaw.config.loader import GovernanceConfig

_LIMIT_FIELDS = ("rate_per_min", "burst", "daily_token_quota", "max_concurrency")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TenantExistsError(Exception):
    """create 时租户已存在。"""


class TenantNotFoundError(Exception):
    """get/update/delete 时租户不存在。"""


def assemble_tenant_record(
    *,
    tenant_id: str,
    display_name: str,
    status: str,
    limits: dict[str, Optional[int]],
    app_ids: list[str],
    metadata: dict[str, Any],
    created_at: str,
    updated_at: str,
) -> dict[str, Any]:
    """组装租户记录的**规范字典形态**（两种后端共用，防止形态漂移）。"""
    return {
        "tenant_id": tenant_id,
        "display_name": display_name,
        "status": status,
        "app_ids": list(app_ids or []),
        "metadata": dict(metadata or {}),
        "created_at": created_at,
        "updated_at": updated_at,
        "limits": {k: limits.get(k) for k in _LIMIT_FIELDS},
    }


class TenantRegistry:
    """租户 CRUD + 配额 + 状态 + app_id 路由（SQLite 持久化）。"""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path) if db_path else (
            Path.home() / ".smartclaw" / "data" / "tenants.db"
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self.initialize()

    def _get_connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self.db_path), check_same_thread=False, timeout=30.0
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def initialize(self) -> None:
        conn = self._get_connection()
        with self._lock:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tenants (
                    tenant_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    rate_per_min INTEGER,
                    burst INTEGER,
                    daily_token_quota INTEGER,
                    max_concurrency INTEGER,
                    app_ids TEXT NOT NULL DEFAULT '[]',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    # ----- 序列化 -----

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        return assemble_tenant_record(
            tenant_id=d["tenant_id"],
            display_name=d.get("display_name") or "",
            status=d.get("status") or "active",
            limits={k: d.get(k) for k in _LIMIT_FIELDS},
            app_ids=json.loads(d.get("app_ids") or "[]"),
            metadata=json.loads(d.get("metadata") or "{}"),
            created_at=d.get("created_at") or "",
            updated_at=d.get("updated_at") or "",
        )

    # ----- CRUD -----

    def create(
        self,
        tenant_id: str,
        *,
        display_name: str = "",
        status: str = "active",
        limits: Optional[dict[str, Optional[int]]] = None,
        app_ids: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        tid = normalize_tenant_id(tenant_id)
        limits = limits or {}
        now = _now_iso()
        conn = self._get_connection()
        with self._lock:
            exists = conn.execute(
                "SELECT 1 FROM tenants WHERE tenant_id = ?", (tid,)
            ).fetchone()
            if exists:
                raise TenantExistsError(tid)
            conn.execute(
                """
                INSERT INTO tenants (
                    tenant_id, display_name, status,
                    rate_per_min, burst, daily_token_quota, max_concurrency,
                    app_ids, metadata, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tid,
                    display_name or "",
                    _normalize_status(status),
                    _opt_int(limits.get("rate_per_min")),
                    _opt_int(limits.get("burst")),
                    _opt_int(limits.get("daily_token_quota")),
                    _opt_int(limits.get("max_concurrency")),
                    json.dumps(sorted(set(app_ids or [])), ensure_ascii=False),
                    json.dumps(metadata or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            conn.commit()
        return self.get(tid)

    def get(self, tenant_id: str) -> dict[str, Any]:
        tid = normalize_tenant_id(tenant_id)
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM tenants WHERE tenant_id = ?", (tid,)
        ).fetchone()
        if not row:
            raise TenantNotFoundError(tid)
        return self._row_to_dict(row)

    def get_or_none(self, tenant_id: str) -> Optional[dict[str, Any]]:
        try:
            return self.get(tenant_id)
        except TenantNotFoundError:
            return None

    def list(self) -> list[dict[str, Any]]:
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT * FROM tenants ORDER BY created_at ASC, tenant_id ASC"
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def update(
        self,
        tenant_id: str,
        *,
        display_name: Optional[str] = None,
        status: Optional[str] = None,
        limits: Optional[dict[str, Optional[int]]] = None,
        app_ids: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        tid = normalize_tenant_id(tenant_id)
        conn = self._get_connection()
        with self._lock:
            row = conn.execute(
                "SELECT 1 FROM tenants WHERE tenant_id = ?", (tid,)
            ).fetchone()
            if not row:
                raise TenantNotFoundError(tid)
            sets: list[str] = []
            params: list[Any] = []
            if display_name is not None:
                sets.append("display_name = ?")
                params.append(display_name)
            if status is not None:
                sets.append("status = ?")
                params.append(_normalize_status(status))
            if limits is not None:
                for k in _LIMIT_FIELDS:
                    if k in limits:
                        sets.append(f"{k} = ?")
                        params.append(_opt_int(limits.get(k)))
            if app_ids is not None:
                sets.append("app_ids = ?")
                params.append(json.dumps(sorted(set(app_ids)), ensure_ascii=False))
            if metadata is not None:
                sets.append("metadata = ?")
                params.append(json.dumps(metadata, ensure_ascii=False))
            sets.append("updated_at = ?")
            params.append(_now_iso())
            params.append(tid)
            conn.execute(
                f"UPDATE tenants SET {', '.join(sets)} WHERE tenant_id = ?", params
            )
            conn.commit()
        return self.get(tid)

    def set_status(self, tenant_id: str, status: str) -> dict[str, Any]:
        return self.update(tenant_id, status=status)

    def delete(self, tenant_id: str) -> None:
        tid = normalize_tenant_id(tenant_id)
        conn = self._get_connection()
        with self._lock:
            cur = conn.execute("DELETE FROM tenants WHERE tenant_id = ?", (tid,))
            conn.commit()
            if cur.rowcount == 0:
                raise TenantNotFoundError(tid)

    # ----- 业务查询（供解析 / 治理消费） -----

    def resolve_by_app_id(self, app_id: str) -> Optional[str]:
        """按 app_id 找租户；命中返回 tenant_id，未命中返回 None。"""
        if not app_id:
            return None
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT tenant_id, app_ids FROM tenants"
        ).fetchall()
        for r in rows:
            try:
                if app_id in json.loads(r["app_ids"] or "[]"):
                    return r["tenant_id"]
            except (json.JSONDecodeError, TypeError):
                continue
        return None

    def is_suspended(self, tenant_id: str) -> bool:
        rec = self.get_or_none(tenant_id)
        return bool(rec and rec.get("status") == "suspended")

    def effective_limits(self, tenant_id: str) -> Optional[dict[str, Optional[int]]]:
        """返回该租户的限额覆盖（仅含非 NULL 项）；无记录返回 None。"""
        rec = self.get_or_none(tenant_id)
        if rec is None:
            return None
        out = {k: v for k, v in rec["limits"].items() if v is not None}
        return out

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _opt_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    return int(v)


def _normalize_status(status: str) -> str:
    s = (status or "").strip().lower()
    if s not in ("active", "suspended"):
        raise ValueError(f"非法 status={status!r}（应为 active|suspended）")
    return s


_REGISTRY_REDIS_HELP = (
    "租户注册表配置为 Redis 后端（控制面多副本一致），但无法使用 Redis：\n"
    "  • 启动 Redis:  docker run -d --name redis -p 6379:6379 redis:7-alpine\n"
    "  • 安装客户端:  uv pip install \".[redis]\"\n"
    "  • 配置(config.toml): [governance] store=\"redis\" "
    'redis_url="redis://127.0.0.1:6379/0"\n'
    "说明：注册表后端跟随 governance.store——memory→本地 SQLite（单副本正确）；"
    "redis→共享后端，使「开通/停用/配额」在多 worker/多副本间一致。"
)


def create_tenant_registry(gov: Optional["GovernanceConfig"] = None) -> Any:
    """按配置选择租户注册表后端（与 governance.store 保持一致）。

    - ``store=="redis"`` 且配置了 ``redis_url``：返回 :class:`RedisTenantRegistry`
      （启动期 ``ping`` fail-fast，连不上抛错并给安装/配置指引，**绝不静默降级**，
      否则会出现「A 副本开通的租户 B 副本不认识、停用不跨副本生效」的控制面割裂）。
    - 其他：返回进程内 :class:`TenantRegistry`（本地 SQLite，单副本正确）。
    """
    if gov is not None and getattr(gov, "store", "memory") == "redis":
        redis_url = getattr(gov, "redis_url", "") or ""
        if not redis_url:
            raise ValueError(
                "governance.store=redis 但未配置 governance.redis_url。\n"
                + _REGISTRY_REDIS_HELP
            )
        try:
            from smartclaw.tenancy.redis_registry import RedisTenantRegistry

            reg = RedisTenantRegistry(redis_url)
            reg.ping()
            return reg
        except (ValueError,):
            raise
        except Exception as exc:  # 连接/认证/超时/缺依赖
            raise RuntimeError(
                f"无法连接 Redis 租户注册表（redis_url={redis_url!r}）：{exc}\n"
                + _REGISTRY_REDIS_HELP
            ) from exc
    return TenantRegistry()


# ----- 进程级单例 -----

_registry: Optional[Any] = None
_registry_lock = threading.Lock()


def get_tenant_registry() -> Any:
    """获取进程级租户注册表单例（后端按 governance.store 选择，redis 连不上 fail-fast）。"""
    global _registry
    with _registry_lock:
        if _registry is None:
            try:
                from smartclaw.config.loader import get_config

                gov = getattr(get_config(), "governance", None)
            except Exception:
                gov = None
            _registry = create_tenant_registry(gov)
        return _registry


def reset_tenant_registry() -> None:
    """重置单例（测试 / 配置热重载后调用）。"""
    global _registry
    with _registry_lock:
        if _registry is not None:
            try:
                _registry.close()
            except Exception:
                pass
        _registry = None
