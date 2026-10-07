"""
Database initializer: creates all tables and seeds subscription plans.
Called once on app startup (idempotent).
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import engine, Base
from app.db.models import SubscriptionPlan

SEED_PLANS = [
    {
        "name": "free",
        "display_name": "Free",
        "price_monthly": 0,
        "price_yearly": 0,
        "requests_per_min": 10,
        "requests_per_day": 500,
        "requests_per_month": 5_000,
        "max_api_keys": 1,
        "features": {"support": "community", "analytics": False, "webhooks": False},
    },
    {
        "name": "starter",
        "display_name": "Starter",
        "price_monthly": 19.00,
        "price_yearly": 190.00,
        "requests_per_min": 60,
        "requests_per_day": 5_000,
        "requests_per_month": 50_000,
        "max_api_keys": 3,
        "features": {"support": "email", "analytics": True, "webhooks": False},
    },
    {
        "name": "pro",
        "display_name": "Pro",
        "price_monthly": 79.00,
        "price_yearly": 790.00,
        "requests_per_min": 300,
        "requests_per_day": 50_000,
        "requests_per_month": 500_000,
        "max_api_keys": 10,
        "features": {"support": "priority", "analytics": True, "webhooks": True},
    },
]


async def init_db():
    """Create tables and seed plans (safe to run multiple times)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    from app.db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        for plan_data in SEED_PLANS:
            result = await session.execute(
                select(SubscriptionPlan).where(SubscriptionPlan.name == plan_data["name"])
            )
            existing = result.scalar_one_or_none()
            if not existing:
                plan = SubscriptionPlan(**plan_data)
                session.add(plan)
        await session.commit()
