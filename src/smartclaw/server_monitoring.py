"""
监控 API 端点

提供 token 使用统计、健康检查等监控接口。
"""

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from smartclaw.auth.platform import PlatformAuthAdapter
from smartclaw.config.loader import get_config
from smartclaw.monitoring.metrics import get_execution_counters, get_token_tracker

router = APIRouter(prefix="/api/monitoring", tags=["monitoring"])


async def require_monitoring_auth(
    authorization: Optional[str] = Header(None, alias="Authorization"),
) -> None:
    cfg = get_config()
    if not PlatformAuthAdapter.verify_monitoring_bearer(authorization, cfg):
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.get("/health")
async def health_check():
    """
    健康检查端点

    返回服务健康状态。
    """
    return {
        "status": "healthy",
        "service": "smartclaw",
        "version": "0.1.0",
    }


@router.get("/ready")
async def readiness_check():
    """
    就绪检查端点

    检查服务是否已准备好接收请求。
    """
    # TODO: 检查数据库连接、LLM 连接等
    return {
        "ready": True,
        "checks": {
            "database": "ok",
            "llm": "ok",
        },
    }


@router.get("/token-stats", dependencies=[Depends(require_monitoring_auth)])
async def get_token_stats(
    agent_id: Optional[str] = Query(None, description="过滤 Agent ID"),
    provider: Optional[str] = Query(None, description="过滤提供商"),
    start_date: Optional[str] = Query(None, description="开始日期 YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="结束日期 YYYY-MM-DD"),
):
    """
    获取 token 使用统计

    返回 token 使用情况的统计数据。
    """
    tracker = get_token_tracker()

    stats = tracker.get_stats(
        agent_id=agent_id,
        provider=provider,
        start_date=start_date,
        end_date=end_date,
    )

    return stats


@router.get("/daily-usage", dependencies=[Depends(require_monitoring_auth)])
async def get_daily_usage(
    agent_id: Optional[str] = Query(None, description="过滤 Agent ID"),
    days: int = Query(7, ge=1, le=90, description="查询最近多少天"),
):
    """
    获取每日使用量

    返回每日的 token 使用情况。
    """
    tracker = get_token_tracker()

    usage = tracker.get_daily_usage(agent_id=agent_id, days=days)

    return {
        "days": days,
        "usage": usage,
    }


@router.get("/agent-usage/{agent_id}", dependencies=[Depends(require_monitoring_auth)])
async def get_agent_usage(
    agent_id: str,
    days: int = Query(7, ge=1, le=90, description="查询最近多少天"),
):
    """
    获取指定 Agent 的使用情况

    返回单个 Agent 的 token 使用统计。
    """
    tracker = get_token_tracker()

    stats = tracker.get_stats(agent_id=agent_id)
    daily = tracker.get_daily_usage(agent_id=agent_id, days=days)

    return {
        "agent_id": agent_id,
        "stats": stats,
        "daily": daily,
    }


@router.get("/execution-stats", dependencies=[Depends(require_monitoring_auth)])
async def get_execution_stats():
    """执行路径内存计数（planner / deepagents / react / fallback）。"""
    return {"counters": get_execution_counters()}


@router.get("/governance/{tenant_id}", dependencies=[Depends(require_monitoring_auth)])
async def get_governance_snapshot(tenant_id: str):
    """租户治理快照：当前生效限额与当日 token 用量（限流/配额/并发）。"""
    from smartclaw.governance import get_governor

    return get_governor().snapshot(tenant_id)


@router.get(
    "/governance/{tenant_id}/user/{open_id}",
    dependencies=[Depends(require_monitoring_auth)],
)
async def get_user_governance_snapshot(tenant_id: str, open_id: str):
    """用户治理快照：某飞书用户在该租户下的生效限额与当日 token 用量。"""
    from smartclaw.governance import get_governor

    return get_governor().user_snapshot(tenant_id, open_id)
