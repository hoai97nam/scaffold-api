"""
Dependencies injected into route handlers:
  - get_current_user_jwt: resolves Bearer JWT → User
  - get_current_user_api_key: resolves X-API-Key header → User + plan context
  - get_redis: provides Redis client from app.state
"""
import json
from typing import Annotated

import jwt as pyjwt
from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.security import decode_token, hash_api_key
from app.db.session import get_db
from app.db.models import User, APIKey, UserSubscription


# ── Redis ────────────────────────────────────────────────────────────────────

async def get_redis(request: Request):
    return request.app.state.redis


# ── JWT auth (Dashboard / management endpoints) ───────────────────────────────

async def get_current_user_jwt(
    authorization: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> User:
    """
    Validates Bearer JWT.
    Checks Redis blacklist for logged-out tokens.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token required")

    token = authorization.split(" ", 1)[1]

    # Check JWT blacklist (logout invalidation)
    is_blacklisted = await redis.get(f"jwt_blacklist:{token}")
    if is_blacklisted:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has been revoked")

    try:
        payload = decode_token(token)
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except pyjwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    if payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not an access token")

    user_id = payload.get("sub")
    result = await db.execute(
        select(User)
        .options(
            selectinload(User.subscriptions).selectinload(UserSubscription.plan),
            selectinload(User.api_keys),
        )
        .where(User.id == user_id, User.is_active == True)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    return user


# ── API Key auth (Business API endpoints) ─────────────────────────────────────

async def get_current_user_api_key(
    request: Request,
    x_api_key: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> dict:
    """
    Validates X-API-Key.
    Uses Redis cache (5-min TTL) to avoid hitting DB on every request.
    Returns a context dict: {user_id, api_key_id, plan, limits}
    """
    if not x_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="X-API-Key header required")

    key_hash = hash_api_key(x_api_key)
    cache_key = f"apikey:{key_hash}"

    # 1. Check Redis cache
    cached = await redis.get(cache_key)
    if cached == b"invalid":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or revoked API key")
    if cached:
        return json.loads(cached)

    # 2. DB lookup (cache miss)
    result = await db.execute(
        select(APIKey)
        .options(
            selectinload(APIKey.user).selectinload(User.subscriptions).selectinload(UserSubscription.plan)
        )
        .where(APIKey.key_hash == key_hash, APIKey.is_active == True)
    )
    key_record = result.scalar_one_or_none()

    if not key_record:
        # Cache negative result 60s (prevents brute-force DB hammering)
        await redis.setex(cache_key, 60, b"invalid")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or revoked API key")

    # Get active subscription
    active_sub = next(
        (s for s in key_record.user.subscriptions if s.status in ("active", "trialing")),
        None,
    )
    if not active_sub:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail={
            "code": "SUBSCRIPTION_REQUIRED",
            "message": "No active subscription. Please subscribe to use the API.",
        })

    plan = active_sub.plan
    context = {
        "user_id": str(key_record.user_id),
        "api_key_id": str(key_record.id),
        "plan": plan.name,
        "limits": {
            "rpm": plan.requests_per_min,
            "rpd": plan.requests_per_day,
            "rpm_month": plan.requests_per_month,
        },
    }
    # Cache valid context for 5 minutes
    await redis.setex(cache_key, 300, json.dumps(context).encode())
    return context
