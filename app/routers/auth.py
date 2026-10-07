"""
Auth router — Register, Login, Logout, Token Refresh

Flow:
  POST /auth/register → creates user + auto-assigns free plan
  POST /auth/login    → returns short-lived access token + refresh token
  POST /auth/logout   → blacklists access token in Redis
  POST /auth/refresh  → validates refresh token, issues new access token
"""
from datetime import datetime, timezone, timedelta

import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.security import hash_password, verify_password, create_access_token, create_refresh_token, decode_token
from app.core.dependencies import get_redis
from app.db.session import get_db
from app.db.models import User, UserSubscription, SubscriptionPlan
from app.schemas.schemas import RegisterRequest, LoginRequest, TokenResponse, RefreshRequest

router = APIRouter(prefix="/auth", tags=["Auth"])


@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new account",
    description=(
        "Creates a new user account and **automatically assigns the Free plan**. "
        "Returns JWT tokens immediately — no email verification required for demo."
    ),
)
async def register(
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    # 1. Check email uniqueness
    existing = await db.execute(select(User).where(User.email == body.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    # 2. Create user
    user = User(
        email=body.email,
        password_hash=hash_password(body.password),
        full_name=body.full_name,
        is_active=True,
        is_verified=False,  # In production: send verification email
    )
    db.add(user)
    await db.flush()  # get user.id without committing

    # 3. Auto-assign Free plan subscription
    free_plan_result = await db.execute(
        select(SubscriptionPlan).where(SubscriptionPlan.name == "free")
    )
    free_plan = free_plan_result.scalar_one_or_none()
    if not free_plan:
        raise HTTPException(status_code=500, detail="Free plan not seeded. Contact admin.")

    now = datetime.now(timezone.utc)
    subscription = UserSubscription(
        user_id=user.id,
        plan_id=free_plan.id,
        status="active",
        billing_cycle="monthly",
        current_period_start=now,
        current_period_end=now + timedelta(days=30),
    )
    db.add(subscription)
    await db.commit()

    # 4. Issue tokens
    access_token = create_access_token(str(user.id), user.email)
    refresh_token = create_refresh_token(str(user.id))

    # Cache refresh token in Redis (7-day TTL) for revocation support
    await redis.setex(
        f"refresh:{str(user.id)}",
        settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        refresh_token,
    )

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Login with email + password",
    description="Authenticates credentials and returns short-lived access token (15 min) + refresh token (7 days).",
)
async def login(
    body: LoginRequest,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    result = await db.execute(select(User).where(User.email == body.email, User.is_active == True))
    user = result.scalar_one_or_none()

    if not user or not verify_password(body.password, user.password_hash):
        # Intentionally vague — don't reveal whether email exists
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    access_token = create_access_token(str(user.id), user.email)
    refresh_token = create_refresh_token(str(user.id))

    # Store refresh token in Redis (allows server-side logout of all sessions)
    await redis.setex(
        f"refresh:{str(user.id)}",
        settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        refresh_token,
    )

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Logout — invalidate current access token",
    description=(
        "Blacklists the access token in Redis until its natural expiry. "
        "After this, the token will be rejected even if not expired."
    ),
)
async def logout(
    request: Request,
    redis=Depends(get_redis),
):
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token required")

    token = auth_header.split(" ", 1)[1]
    try:
        payload = decode_token(token)
        exp = payload.get("exp", 0)
        ttl = max(0, int(exp - datetime.now(timezone.utc).timestamp()))
        if ttl > 0:
            await redis.setex(f"jwt_blacklist:{token}", ttl, b"1")
        # Also delete refresh token
        user_id = payload.get("sub")
        if user_id:
            await redis.delete(f"refresh:{user_id}")
    except pyjwt.InvalidTokenError:
        pass  # Logout of invalid tokens is a no-op

    return None


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Refresh access token",
    description="Validates the refresh token and issues a new access token. Refresh token is rotated.",
)
async def refresh_token(
    body: RefreshRequest,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    try:
        payload = decode_token(body.refresh_token)
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token expired")
    except pyjwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    if payload.get("type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not a refresh token")

    user_id = payload.get("sub")

    # Validate stored refresh token matches (prevents token reuse after logout)
    stored = await redis.get(f"refresh:{user_id}")
    if not stored or stored.decode() != body.refresh_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token invalid or expired")

    result = await db.execute(select(User).where(User.id == user_id, User.is_active == True))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    # Rotate tokens
    new_access = create_access_token(str(user.id), user.email)
    new_refresh = create_refresh_token(str(user.id))
    await redis.setex(
        f"refresh:{user_id}",
        settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        new_refresh,
    )

    return TokenResponse(
        access_token=new_access,
        refresh_token=new_refresh,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
