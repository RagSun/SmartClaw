"""租户生命周期管理 API（``/api/admin/tenants``）。

提供开通 / 查询 / 改配额 / 停用 / 启用 / 删除，使新增租户「运营化」而非改配置重启。
鉴权复用监控共享 Bearer（``auth.monitoring_bearer_token``），不新增配置项。

变更即时生效：治理器与租户解析都「读注册表的实时状态」，无需重启或热重载。
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from smartclaw.auth.platform import PlatformAuthAdapter
from smartclaw.config.loader import get_config
from smartclaw.tenancy.registry import (
    TenantExistsError,
    TenantNotFoundError,
    TenantRegistry,
    get_tenant_registry,
)

router = APIRouter(prefix="/api/admin/tenants", tags=["admin:tenants"])


async def require_admin_auth(
    authorization: Optional[str] = Header(None, alias="Authorization"),
) -> None:
    cfg = get_config()
    if not PlatformAuthAdapter.verify_admin_bearer(authorization, cfg):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _registry() -> TenantRegistry:
    """依赖注入点：测试可用 dependency_overrides 覆盖为临时库。"""
    return get_tenant_registry()


class TenantLimitsModel(BaseModel):
    """限额覆盖；None=继承全局默认，0=该维度不限。"""

    rate_per_min: Optional[int] = Field(default=None, ge=0)
    burst: Optional[int] = Field(default=None, ge=0)
    daily_token_quota: Optional[int] = Field(default=None, ge=0)
    max_concurrency: Optional[int] = Field(default=None, ge=0)


class TenantCreate(BaseModel):
    tenant_id: str = Field(..., min_length=1, description="租户 ID（将被规范化）")
    display_name: str = Field(default="", description="展示名")
    status: str = Field(default="active", description="active | suspended")
    limits: TenantLimitsModel = Field(default_factory=TenantLimitsModel)
    app_ids: list[str] = Field(default_factory=list, description="飞书/企微 app_id 路由")
    metadata: dict[str, Any] = Field(default_factory=dict)


class TenantUpdate(BaseModel):
    display_name: Optional[str] = None
    status: Optional[str] = Field(default=None, description="active | suspended")
    limits: Optional[TenantLimitsModel] = None
    app_ids: Optional[list[str]] = None
    metadata: Optional[dict[str, Any]] = None


@router.post("", status_code=201, dependencies=[Depends(require_admin_auth)])
@router.post("/", status_code=201, dependencies=[Depends(require_admin_auth)])
async def create_tenant(body: TenantCreate, reg: TenantRegistry = Depends(_registry)):
    try:
        return reg.create(
            body.tenant_id,
            display_name=body.display_name,
            status=body.status,
            limits=body.limits.model_dump(),
            app_ids=body.app_ids,
            metadata=body.metadata,
        )
    except TenantExistsError:
        raise HTTPException(status_code=409, detail="tenant already exists")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("", dependencies=[Depends(require_admin_auth)])
@router.get("/", dependencies=[Depends(require_admin_auth)])
async def list_tenants(reg: TenantRegistry = Depends(_registry)):
    return {"tenants": reg.list()}


@router.get("/{tenant_id}", dependencies=[Depends(require_admin_auth)])
async def get_tenant(tenant_id: str, reg: TenantRegistry = Depends(_registry)):
    try:
        return reg.get(tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="tenant not found")


@router.patch("/{tenant_id}", dependencies=[Depends(require_admin_auth)])
async def update_tenant(
    tenant_id: str, body: TenantUpdate, reg: TenantRegistry = Depends(_registry)
):
    try:
        return reg.update(
            tenant_id,
            display_name=body.display_name,
            status=body.status,
            limits=body.limits.model_dump() if body.limits is not None else None,
            app_ids=body.app_ids,
            metadata=body.metadata,
        )
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="tenant not found")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/{tenant_id}/suspend", dependencies=[Depends(require_admin_auth)])
async def suspend_tenant(tenant_id: str, reg: TenantRegistry = Depends(_registry)):
    try:
        return reg.set_status(tenant_id, "suspended")
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="tenant not found")


@router.post("/{tenant_id}/activate", dependencies=[Depends(require_admin_auth)])
async def activate_tenant(tenant_id: str, reg: TenantRegistry = Depends(_registry)):
    try:
        return reg.set_status(tenant_id, "active")
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="tenant not found")


@router.delete("/{tenant_id}", status_code=204, dependencies=[Depends(require_admin_auth)])
async def delete_tenant(tenant_id: str, reg: TenantRegistry = Depends(_registry)):
    try:
        reg.delete(tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="tenant not found")
