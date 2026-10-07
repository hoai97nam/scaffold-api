"""
/me router and Business API demo router

GET /me           → profile + subscription status (JWT auth)
GET /v1/analyze   → demo business endpoint (API Key auth + rate limiting + usage tracking)
GET /v1/process   → another demo business endpoint (API Key auth)

This file demonstrates the full API key → rate limit → quota → business logic → usage tracking pipeline.
"""
import time
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.dependencies import get_current_user_jwt, get_current_user_api_key, get_redis
from app.db.session import get_db
from app.db.models import User, UserSubscription, UsageLog, APIKey
from app.middleware.rate_limit import RateLimiter
from app.schemas.schemas import UserProfile, SubscriptionStatus

me_router = APIRouter(tags=["User"])
api_router = APIRouter(prefix="/v1", tags=["Business API (API Key Auth)"])


# ─────────────────────────────────────────────────────────────────────────────
# /me endpoint
# ─────────────────────────────────────────────────────────────────────────────

@me_router.get(
    "/me",
    response_model=UserProfile,
    summary="Get current user profile",
    description="**JWT required.** Returns user profile with active subscription info.",
)
async def get_me(
    current_user: User = Depends(get_current_user_jwt),
    db: AsyncSession = Depends(get_db),
):
    # Load subscription with plan
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

    subscription_status = None
    if sub:
        subscription_status = SubscriptionStatus(
            plan_name=sub.plan.name,
            display_name=sub.plan.display_name,
            status=sub.status,
            billing_cycle=sub.billing_cycle,
            price_monthly=float(sub.plan.price_monthly),
            current_period_start=sub.current_period_start,
            current_period_end=sub.current_period_end,
            requests_per_min=sub.plan.requests_per_min,
            requests_per_day=sub.plan.requests_per_day,
            requests_per_month=sub.plan.requests_per_month,
        )

    return UserProfile(
        id=current_user.id,
        email=current_user.email,
        full_name=current_user.full_name,
        is_active=current_user.is_active,
        is_verified=current_user.is_verified,
        created_at=current_user.created_at,
        subscription=subscription_status,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Business API: full pipeline demonstration
#   API Key auth → subscription check → rate limiting → quota check →
#   business logic → response → background usage tracking
# ─────────────────────────────────────────────────────────────────────────────

async def _track_usage(
    user_id: str,
    api_key_id: str,
    endpoint: str,
    method: str,
    status_code: int,
    response_time_ms: int,
    redis,
):
    """
    Background task: increments Redis counter + writes usage_log to DB.
    Non-blocking — called after response is sent to client.
    """
    now = datetime.now(timezone.utc)
    period_key = f"usage:{user_id}:{now.year}:{now.month}"

    # Atomic Redis increment (fast, O(1))
    pipe = redis.pipeline()
    pipe.incr(period_key)
    pipe.expire(period_key, 35 * 86400)
    await pipe.execute()

    # Async DB write (audit trail)
    from app.db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        log = UsageLog(
            user_id=user_id,
            api_key_id=api_key_id if api_key_id else None,
            endpoint=endpoint,
            method=method,
            status_code=status_code,
            response_time_ms=response_time_ms,
            period_year=now.year,
            period_month=now.month,
        )
        db.add(log)

        # Update last_used_at on API key
        if api_key_id:
            await db.execute(
                update(APIKey)
                .where(APIKey.id == api_key_id)
                .values(last_used_at=now)
            )
        await db.commit()


@api_router.get(
    "/analyze",
    summary="[Demo] Analyze endpoint — requires API Key",
    description=(
        "**X-API-Key required.** Full pipeline:\n\n"
        "1. API key authenticated via Redis cache / DB\n"
        "2. Subscription validity checked\n"
        "3. Rate limiting enforced (per-minute / per-day / per-month)\n"
        "4. Business logic executed (mock: returns analysis result)\n"
        "5. Usage tracked in background (Redis + DB)\n\n"
        "Try with a `free` plan user — lower limits kick in quickly."
    ),
)
async def analyze(
    request: Request,
    background_tasks: BackgroundTasks,
    query: str = "hello world",
    auth_context: dict = Depends(get_current_user_api_key),
    redis=Depends(get_redis),
):
    start_time = time.time()

    # ① Rate limiting (per-minute / per-day / per-month)
    limiter = RateLimiter(redis)
    await limiter.check(auth_context["user_id"], auth_context["limits"])

    # ② Monthly quota check (atomic Lua script to prevent over-use)
    QUOTA_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if current == false then current = 0 else current = tonumber(current) end
if current >= tonumber(ARGV[1]) then return -1 end
return tonumber(current)
"""
    now = datetime.now(timezone.utc)
    period_key = f"usage:{auth_context['user_id']}:{now.year}:{now.month}"
    count = await redis.eval(QUOTA_SCRIPT, 1, period_key, auth_context["limits"]["rpm_month"])
    if count == -1:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "MONTHLY_QUOTA_EXCEEDED",
                "limit": auth_context["limits"]["rpm_month"],
                "message": "Monthly API quota exceeded. Upgrade your plan for more requests.",
            },
        )

    # ③ Business logic (MOCK — replace with real analysis)
    result = {
        "query": query,
        "sentiment": "positive" if len(query) % 2 == 0 else "neutral",
        "word_count": len(query.split()),
        "confidence": 0.91,
        "plan": auth_context["plan"],
        "meta": {
            "model": "mock-nlp-v1.0",
            "processed_at": datetime.now(timezone.utc).isoformat(),
        },
    }

    elapsed_ms = int((time.time() - start_time) * 1000)

    # ④ Background usage tracking (non-blocking — response already sent)
    background_tasks.add_task(
        _track_usage,
        user_id=auth_context["user_id"],
        api_key_id=auth_context.get("api_key_id"),
        endpoint=request.url.path,
        method=request.method,
        status_code=200,
        response_time_ms=elapsed_ms,
        redis=redis,
    )

    return {
        "ok": True,
        "data": result,
        "response_time_ms": elapsed_ms,
    }


@api_router.get(
    "/process",
    summary="[Demo] Process endpoint — requires API Key",
    description=(
        "**X-API-Key required.** Another business endpoint demonstrating "
        "the same auth + rate-limit + usage pipeline.\n\n"
        "Simulates a data processing job."
    ),
)
async def process(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: str = "sample data",
    auth_context: dict = Depends(get_current_user_api_key),
    redis=Depends(get_redis),
):
    start_time = time.time()

    # Rate limiting
    limiter = RateLimiter(redis)
    await limiter.check(auth_context["user_id"], auth_context["limits"])

    # Mock processing (different feature availability by plan)
    features = {
        "free":    {"max_payload_kb": 10,   "priority": "low",    "output_formats": ["json"]},
        "starter": {"max_payload_kb": 512,  "priority": "normal", "output_formats": ["json", "csv"]},
        "pro":     {"max_payload_kb": 10240,"priority": "high",   "output_formats": ["json", "csv", "parquet"]},
    }
    plan_features = features.get(auth_context["plan"], features["free"])

    # Pro-only feature gate
    if auth_context["plan"] == "free" and len(payload) > 100:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FEATURE_NOT_AVAILABLE",
                "message": "Large payload processing requires Starter or Pro plan. Upgrade to unlock.",
            },
        )

    result = {
        "input_length": len(payload),
        "processed": payload.upper(),
        "checksum": hex(hash(payload) & 0xFFFFFFFF),
        "plan_features": plan_features,
        "plan": auth_context["plan"],
    }

    elapsed_ms = int((time.time() - start_time) * 1000)

    background_tasks.add_task(
        _track_usage,
        user_id=auth_context["user_id"],
        api_key_id=auth_context.get("api_key_id"),
        endpoint=request.url.path,
        method=request.method,
        status_code=200,
        response_time_ms=elapsed_ms,
        redis=redis,
    )

    return {
        "ok": True,
        "data": result,
        "response_time_ms": elapsed_ms,
    }
