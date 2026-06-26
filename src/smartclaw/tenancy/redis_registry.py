"""租户注册表的 Redis 后端（控制面多副本一致）。

把租户身份 / 状态 / 限额覆盖 / app_id 路由下沉到共享 Redis，使「开通 / 停用 /
改配额」在多 worker、多副本间立刻一致——这是 SQLite 本地文件后端做不到的。

键空间（``ns`` 默认 ``fc:tenant``）：

- ``{ns}:{tid}``         → hash，单个租户的全部字段（含 JSON 编码的 app_ids/metadata）
- ``{ns}s``（即 ``fc:tenants``）→ set，全部 tenant_id，供 list 枚举
- ``{ns}_appid``        → hash，``app_id -> tenant_id`` 路由索引（O(1) 解析，
  顺带修掉 SQLite 后端 resolve_by_app_id 的全表扫描）

公开方法签名与 :class:`smartclaw.tenancy.registry.TenantRegistry` 完全一致，
两者可由 :func:`create_tenant_registry` 透明替换。
"""

from __future__ import annotations

import json
from typing import Any, Optional

from smartclaw.tenant import normalize_tenant_id
from smartclaw.tenancy.registry import (
    _LIMIT_FIELDS,
    TenantExistsError,
    TenantNotFoundError,
    _normalize_status,
    _now_iso,
    _opt_int,
    assemble_tenant_record,
)

# 原子创建：存在即返回 0（拒绝重复），否则写入全部字段并加入集合，返回 1。
_LUA_CREATE = """
local hkey = KEYS[1]
local setkey = KEYS[2]
local tid = ARGV[1]
if redis.call('EXISTS', hkey) == 1 then
  return 0
end
for i = 2, #ARGV, 2 do
  redis.call('HSET', hkey, ARGV[i], ARGV[i + 1])
end
redis.call('SADD', setkey, tid)
return 1
"""


def _enc_int(v: Any) -> str:
    """可选整型 → 存储串：None 存为空串。"""
    iv = _opt_int(v)
    return "" if iv is None else str(iv)


def _dec_int(s: Optional[str]) -> Optional[int]:
    if s is None or s == "":
        return None
    return int(s)


class RedisTenantRegistry:
    """租户 CRUD + 配额 + 状态 + app_id 路由（Redis 共享后端）。"""

    def __init__(
        self,
        redis_url: str = "",
        *,
        client: Any = None,
        namespace: str = "fc:tenant",
    ) -> None:
        if client is None:
            import redis  # 惰性导入，仅 redis 后端才需要

            client = redis.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=3,
            )
        self._r = client
        self._ns = namespace.rstrip(":")
        self._set_key = f"{self._ns}s"          # fc:tenants
        self._appid_key = f"{self._ns}_appid"   # fc:tenant_appid
        self._create_script = client.register_script(_LUA_CREATE)

    # ----- 基础设施 -----

    def ping(self) -> bool:
        return bool(self._r.ping())

    def _hkey(self, tid: str) -> str:
        return f"{self._ns}:{tid}"

    def initialize(self) -> None:  # 接口对齐 SQLite 后端；Redis 无需建表
        return None

    # ----- 编解码 -----

    def _record_from_hash(self, tid: str, h: dict[str, str]) -> dict[str, Any]:
        return assemble_tenant_record(
            tenant_id=tid,
            display_name=h.get("display_name") or "",
            status=h.get("status") or "active",
            limits={k: _dec_int(h.get(k)) for k in _LIMIT_FIELDS},
            app_ids=json.loads(h.get("app_ids") or "[]"),
            metadata=json.loads(h.get("metadata") or "{}"),
            created_at=h.get("created_at") or "",
            updated_at=h.get("updated_at") or "",
        )

    def _index_app_ids(self, tid: str, add: list[str], remove: list[str]) -> None:
        pipe = self._r.pipeline()
        for aid in remove:
            if aid:
                pipe.hdel(self._appid_key, aid)
        for aid in add:
            if aid:
                pipe.hset(self._appid_key, aid, tid)
        pipe.execute()

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
        app_id_list = sorted(set(app_ids or []))
        fields = {
            "display_name": display_name or "",
            "status": _normalize_status(status),
            "app_ids": json.dumps(app_id_list, ensure_ascii=False),
            "metadata": json.dumps(metadata or {}, ensure_ascii=False),
            "created_at": now,
            "updated_at": now,
        }
        for k in _LIMIT_FIELDS:
            fields[k] = _enc_int(limits.get(k))
        args: list[str] = [tid]
        for k, v in fields.items():
            args.extend([k, v])
        created = self._create_script(keys=[self._hkey(tid), self._set_key], args=args)
        if int(created) == 0:
            raise TenantExistsError(tid)
        self._index_app_ids(tid, add=app_id_list, remove=[])
        return self.get(tid)

    def get(self, tenant_id: str) -> dict[str, Any]:
        tid = normalize_tenant_id(tenant_id)
        h = self._r.hgetall(self._hkey(tid))
        if not h:
            raise TenantNotFoundError(tid)
        return self._record_from_hash(tid, h)

    def get_or_none(self, tenant_id: str) -> Optional[dict[str, Any]]:
        try:
            return self.get(tenant_id)
        except TenantNotFoundError:
            return None

    def list(self) -> list[dict[str, Any]]:
        tids = self._r.smembers(self._set_key)
        records: list[dict[str, Any]] = []
        for tid in tids:
            rec = self.get_or_none(tid)
            if rec is not None:
                records.append(rec)
        records.sort(key=lambda r: (r.get("created_at") or "", r.get("tenant_id") or ""))
        return records

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
        hkey = self._hkey(tid)
        current = self._r.hgetall(hkey)
        if not current:
            raise TenantNotFoundError(tid)
        updates: dict[str, str] = {}
        if display_name is not None:
            updates["display_name"] = display_name
        if status is not None:
            updates["status"] = _normalize_status(status)
        if limits is not None:
            for k in _LIMIT_FIELDS:
                if k in limits:
                    updates[k] = _enc_int(limits.get(k))
        old_app_ids = json.loads(current.get("app_ids") or "[]")
        new_app_ids = old_app_ids
        if app_ids is not None:
            new_app_ids = sorted(set(app_ids))
            updates["app_ids"] = json.dumps(new_app_ids, ensure_ascii=False)
        if metadata is not None:
            updates["metadata"] = json.dumps(metadata, ensure_ascii=False)
        updates["updated_at"] = _now_iso()
        self._r.hset(hkey, mapping=updates)
        if app_ids is not None:
            removed = [a for a in old_app_ids if a not in new_app_ids]
            added = [a for a in new_app_ids if a not in old_app_ids]
            self._index_app_ids(tid, add=added, remove=removed)
        return self.get(tid)

    def set_status(self, tenant_id: str, status: str) -> dict[str, Any]:
        return self.update(tenant_id, status=status)

    def delete(self, tenant_id: str) -> None:
        tid = normalize_tenant_id(tenant_id)
        hkey = self._hkey(tid)
        current = self._r.hgetall(hkey)
        if not current:
            raise TenantNotFoundError(tid)
        old_app_ids = json.loads(current.get("app_ids") or "[]")
        pipe = self._r.pipeline()
        pipe.delete(hkey)
        pipe.srem(self._set_key, tid)
        for aid in old_app_ids:
            if aid:
                pipe.hdel(self._appid_key, aid)
        pipe.execute()

    # ----- 业务查询 -----

    def resolve_by_app_id(self, app_id: str) -> Optional[str]:
        if not app_id:
            return None
        return self._r.hget(self._appid_key, app_id)

    def is_suspended(self, tenant_id: str) -> bool:
        tid = normalize_tenant_id(tenant_id)
        status = self._r.hget(self._hkey(tid), "status")
        return status == "suspended"

    def effective_limits(self, tenant_id: str) -> Optional[dict[str, Optional[int]]]:
        rec = self.get_or_none(tenant_id)
        if rec is None:
            return None
        return {k: v for k, v in rec["limits"].items() if v is not None}

    def close(self) -> None:
        try:
            self._r.close()
        except Exception:
            pass


__all__ = ["RedisTenantRegistry"]
