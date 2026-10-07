"""
Usage router

GET /usage/  → usage stats for current billing period (JWT auth)

Returns:
  - Total requests this month
  - Monthly limit
  - Remaining requests
  - % used
  - Recent 10 log entries

Usage tracking is done via:
  1. Redis atomic INCR counter (fast, updated on every API request)
  2. PostgreSQL usage_logs for audit trail (written async via background task)
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.dependencies import get_current_user_jwt, get_redis
from app.db.session import get_db
from app.db.models import User, UsageLog, UserSubscription, SubscriptionPlan
from app.schemas.schemas import UsageStats, UsageLogOut

router = APIRouter(prefix="/usage", tags=["Usage"])


@router.get(
    "/",
    response_model=UsageStats,
    summary="Get usage stats for current billing period",
    description=(
        "**JWT required.** Shows current month's request count from Redis counter "
        "(fast, always consistent) plus recent log entries from PostgreSQL."
    ),
)
async def get_usage(
    current_user: User = Depends(get_current_user_jwt),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    # 1. Get active subscription + plan limits
    sub_result = await db.execute(
        select(UserSubscription)
        .options(selectinload(UserSubscription.plan))
        .where(
            UserSubscription.user_id == current_user.id,
            UserSubscription.status.in_(["active", "trialing"]),
        )
        .order_by(UserSubscription.created_at.desc())
    )
    sub = sub_result.scalars().first()
    if not sub:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No active subscription found")

    plan = sub.plan
    now = datetime.now(timezone.utc)
    period_key = f"usage:{str(current_user.id)}:{now.year}:{now.month}"

    # 2. Get counter from Redis (primary source, fastest)
    redis_count = await redis.get(period_key)
    if redis_count is not None:
        total = int(redis_count)
    else:
        # Cold start: load from DB usage_counters
        count_result = await db.execute(
            select(func.count(UsageLog.id))
            .where(
                UsageLog.user_id == current_user.id,
                UsageLog.period_year == now.year,
                UsageLog.period_month == now.month,
            )
        )
        total = count_result.scalar() or 0
        # Warm Redis
        await redis.setex(period_key, 35 * 86400, total)

    monthly_limit = plan.requests_per_month
    remaining = max(0, monthly_limit - total)
    percent_used = round((total / monthly_limit) * 100, 2) if monthly_limit > 0 else 0.0

    # 3. Fetch recent 10 log entries for transparency
    logs_result = await db.execute(
        select(UsageLog)
        .where(
            UsageLog.user_id == current_user.id,
            UsageLog.period_year == now.year,
            UsageLog.period_month == now.month,
        )
        .order_by(UsageLog.created_at.desc())
        .limit(10)
    )
    recent_logs = logs_result.scalars().all()

    return UsageStats(
        period=f"{now.year}-{now.month:02d}",
        total_requests=total,
        monthly_limit=monthly_limit,
        remaining=remaining,
        percent_used=percent_used,
        daily_limit=plan.requests_per_day,
        per_minute_limit=plan.requests_per_min,
        recent_logs=[
            UsageLogOut(
                endpoint=log.endpoint,
                method=log.method,
                status_code=log.status_code,
                response_time_ms=log.response_time_ms,
                created_at=log.created_at,
            )
            for log in recent_logs
        ],
    )
