"""
API Keys router

GET    /api-keys/          → list user's API keys (JWT auth)
POST   /api-keys/          → create new API key (JWT auth, plan limit enforced)
DELETE /api-keys/{key_id}  → revoke API key (JWT auth)

Business logic:
  - Raw key shown ONCE on creation; only SHA-256 hash stored in DB
  - Max keys enforced per plan (SELECT FOR UPDATE prevents race conditions)
  - Revoking deletes Redis cache → immediate invalidation (< 1 second)
  - Audit log entry written on creation and revocation
"""
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_user_jwt, get_redis
from app.core.security import generate_api_key, hash_api_key
from app.db.session import get_db
from app.db.models import User, APIKey, UserSubscription, SubscriptionPlan, AuditLog
from app.schemas.schemas import APIKeyOut, APIKeyCreated, CreateAPIKeyRequest

router = APIRouter(prefix="/api-keys", tags=["API Keys"])


@router.get(
    "/",
    response_model=list[APIKeyOut],
    summary="List all active API keys",
    description="**JWT required.** Returns all non-revoked keys. Raw keys are never returned here.",
)
async def list_api_keys(
    current_user: User = Depends(get_current_user_jwt),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(APIKey)
        .where(APIKey.user_id == current_user.id, APIKey.is_active == True)
        .order_by(APIKey.created_at.desc())
    )
    return result.scalars().all()


@router.post(
    "/",
    response_model=APIKeyCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new API key",
    description=(
        "**JWT required.** Generates a new API key.\n\n"
        "⚠️ **The `raw_key` is shown ONCE** — store it securely. "
        "It cannot be recovered; you must revoke and create a new one.\n\n"
        "Enforces the plan's `max_api_keys` limit using row-level locking."
    ),
)
async def create_api_key(
    body: CreateAPIKeyRequest,
    request: Request,
    current_user: User = Depends(get_current_user_jwt),
    db: AsyncSession = Depends(get_db),
):
    # 1. Get active subscription + plan limits
    sub_result = await db.execute(
        select(UserSubscription)
        .join(SubscriptionPlan)
        .where(
            UserSubscription.user_id == current_user.id,
            UserSubscription.status.in_(["active", "trialing"]),
        )
    )
    sub = sub_result.scalars().first()
    if not sub:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail="No active subscription")

    plan_result = await db.execute(select(SubscriptionPlan).where(SubscriptionPlan.id == sub.plan_id))
    plan = plan_result.scalar_one()

    # 2. Check key count with row lock (prevents race condition on concurrent creation)
    async with db.begin_nested():
        # Lock the user row
        await db.execute(
            select(User).where(User.id == current_user.id).with_for_update()
        )
        count_result = await db.execute(
            select(func.count(APIKey.id))
            .where(APIKey.user_id == current_user.id, APIKey.is_active == True)
        )
        current_key_count = count_result.scalar()

        if current_key_count >= plan.max_api_keys:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "MAX_API_KEYS_REACHED",
                    "current": current_key_count,
                    "limit": plan.max_api_keys,
                    "message": f"Your {plan.display_name} plan allows a maximum of {plan.max_api_keys} API key(s). Revoke an existing key or upgrade your plan.",
                },
            )

        # 3. Generate key
        raw_key, key_hash, key_prefix = generate_api_key()

        new_key = APIKey(
            user_id=current_user.id,
            key_hash=key_hash,
            key_prefix=key_prefix,
            name=body.name,
            is_active=True,
        )
        db.add(new_key)
        await db.flush()

        # 4. Audit log
        audit = AuditLog(
            user_id=current_user.id,
            actor_ip=request.client.host if request.client else None,
            event_type="key_created",
            entity_type="api_key",
            entity_id=str(new_key.id),
            metadata_={"name": body.name, "key_prefix": key_prefix},
        )
        db.add(audit)

    await db.commit()
    await db.refresh(new_key)

    return APIKeyCreated(
        id=new_key.id,
        name=new_key.name,
        key_prefix=new_key.key_prefix,
        is_active=new_key.is_active,
        created_at=new_key.created_at,
        last_used_at=new_key.last_used_at,
        expires_at=new_key.expires_at,
        raw_key=raw_key,  # ← shown ONCE
    )


@router.delete(
    "/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an API key",
    description=(
        "**JWT required.** Marks the key as revoked and immediately deletes its Redis cache entry. "
        "Any in-flight requests using this key will fail within seconds."
    ),
)
async def revoke_api_key(
    key_id: UUID,
    request: Request,
    current_user: User = Depends(get_current_user_jwt),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    result = await db.execute(
        select(APIKey).where(APIKey.id == key_id, APIKey.user_id == current_user.id)
    )
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    if not key.is_active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Key is already revoked")

    # Revoke
    key.is_active = False
    key.revoked_at = datetime.now(timezone.utc)

    # Audit log
    audit = AuditLog(
        user_id=current_user.id,
        actor_ip=request.client.host if request.client else None,
        event_type="key_revoked",
        entity_type="api_key",
        entity_id=str(key.id),
        metadata_={"name": key.name, "key_prefix": key.key_prefix},
    )
    db.add(audit)
    await db.commit()

    # Immediate Redis cache invalidation — revocation takes effect within seconds, not 5 minutes
    await redis.delete(f"apikey:{key.key_hash}")

    return None
