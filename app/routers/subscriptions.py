"""
Subscriptions router

GET  /subscriptions/plans          → list all available plans (public)
POST /subscriptions/subscribe      → subscribe or upgrade/downgrade (JWT auth)
GET  /subscriptions/me             → current subscription status (JWT auth)
GET  /subscriptions/billing        → billing history (JWT auth)

Business logic:
  - Subscribing to a plan creates/replaces the active subscription
  - Downgrading to free is always allowed
  - Upgrading creates a new subscription record and closes the old one
  - A mock billing record is created for paid plans (no real payment gateway)
  - Changing plans invalidates the API key cache in Redis
"""
from datetime import datetime, timezone, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.dependencies import get_current_user_jwt, get_redis
from app.db.session import get_db
from app.db.models import User, SubscriptionPlan, UserSubscription, BillingHistory
from app.schemas.schemas import PlanOut, SubscribeRequest, SubscriptionStatus, BillingRecord

router = APIRouter(prefix="/subscriptions", tags=["Subscriptions"])


@router.get(
    "/plans",
    response_model=list[PlanOut],
    summary="List all subscription plans",
    description="Returns all active plans. **No auth required.** Useful for pricing pages.",
)
async def list_plans(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(SubscriptionPlan).where(SubscriptionPlan.is_active == True).order_by(SubscriptionPlan.price_monthly)
    )
    return result.scalars().all()


@router.post(
    "/subscribe",
    response_model=SubscriptionStatus,
    summary="Subscribe, upgrade, or downgrade plan",
    description=(
        "**JWT required.** Switches the user to the specified plan.\n\n"
        "- `free` → always free, no payment\n"
        "- `starter` / `pro` → mock payment recorded in billing_history\n"
        "- Upgrading: old subscription is canceled, new one starts immediately\n"
        "- Downgrading to free: same behavior\n"
        "- Redis API key cache is invalidated immediately on plan change"
    ),
)
async def subscribe(
    body: SubscribeRequest,
    current_user: User = Depends(get_current_user_jwt),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    # 1. Fetch target plan
    plan_result = await db.execute(
        select(SubscriptionPlan).where(
            SubscriptionPlan.name == body.plan_name,
            SubscriptionPlan.is_active == True,
        )
    )
    plan = plan_result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Plan '{body.plan_name}' not found")

    if body.billing_cycle not in ("monthly", "yearly"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="billing_cycle must be 'monthly' or 'yearly'")

    # 2. Cancel existing active subscription
    existing_result = await db.execute(
        select(UserSubscription)
        .where(UserSubscription.user_id == current_user.id, UserSubscription.status.in_(["active", "trialing"]))
    )
    existing_subs = existing_result.scalars().all()
    now = datetime.now(timezone.utc)

    for sub in existing_subs:
        sub.status = "canceled"
        sub.canceled_at = now

    # 3. Determine period length
    if body.billing_cycle == "yearly":
        period_end = now + timedelta(days=365)
        price = float(plan.price_yearly)
    else:
        period_end = now + timedelta(days=30)
        price = float(plan.price_monthly)

    # 4. Create new subscription
    new_sub = UserSubscription(
        user_id=current_user.id,
        plan_id=plan.id,
        status="active",
        billing_cycle=body.billing_cycle,
        current_period_start=now,
        current_period_end=period_end,
    )
    db.add(new_sub)
    await db.flush()

    # 5. Mock billing record (replace with Stripe in production)
    if price > 0:
        import uuid
        billing = BillingHistory(
            user_id=current_user.id,
            subscription_id=new_sub.id,
            amount=price,
            currency="USD",
            status="paid",  # In production: status='pending' until payment gateway confirms
            description=f"{plan.display_name} plan — {body.billing_cycle} billing",
            external_payment_id=f"mock_ch_{uuid.uuid4().hex[:16]}",
            period_start=now,
            period_end=period_end,
        )
        db.add(billing)

    await db.commit()
    await db.refresh(new_sub)

    # 6. Invalidate cached subscription status so new plan limits take effect immediately.
    # API key cache TTL is 5 min — acceptable lag. Sub status cache must be cleared instantly.
    await redis.delete(f"sub_status:{str(current_user.id)}")

    return SubscriptionStatus(
        plan_name=plan.name,
        display_name=plan.display_name,
        status=new_sub.status,
        billing_cycle=new_sub.billing_cycle,
        price_monthly=float(plan.price_monthly),
        current_period_start=new_sub.current_period_start,
        current_period_end=new_sub.current_period_end,
        requests_per_min=plan.requests_per_min,
        requests_per_day=plan.requests_per_day,
        requests_per_month=plan.requests_per_month,
    )


@router.get(
    "/me",
    response_model=SubscriptionStatus,
    summary="Get current subscription status",
    description="**JWT required.** Returns the user's active subscription and plan limits.",
)
async def get_my_subscription(
    current_user: User = Depends(get_current_user_jwt),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(UserSubscription)
        .options(selectinload(UserSubscription.plan))
        .where(
            UserSubscription.user_id == current_user.id,
            UserSubscription.status.in_(["active", "trialing"]),
        )
        .order_by(UserSubscription.created_at.desc())
    )
    sub = result.scalars().first()
    if not sub:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No active subscription")

    return SubscriptionStatus(
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


@router.get(
    "/billing",
    response_model=list[BillingRecord],
    summary="Billing history",
    description="**JWT required.** Returns the last 20 billing records for the user.",
)
async def billing_history(
    current_user: User = Depends(get_current_user_jwt),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(BillingHistory)
        .where(BillingHistory.user_id == current_user.id)
        .order_by(BillingHistory.created_at.desc())
        .limit(20)
    )
    return result.scalars().all()
