"""
Subscription Celery tasks (run in celery_worker, scheduled by celery_beat).

Tasks:
  process_expired_subscriptions()  — daily at 00:05 UTC
    Finds subscriptions in 'past_due' that are past the 3-day grace period.
    Downgrades them to the Free plan instead of hard-blocking (reduces churn).
    Writes an audit log entry per user affected.

  reset_monthly_counters()         — 1st of each month at 00:05 UTC
    Syncs DB usage_counters with Redis values before Redis TTL clears them.
    Redis counters expire naturally after 35 days; this captures the final
    value for billing reports before rollover.

  send_expiry_reminders()          — daily, 7 days before period_end
    (Stub — replace with real email provider like SendGrid/Resend.)
"""
import logging
from datetime import datetime, timezone, timedelta

from celery import shared_task
from sqlalchemy import select, update

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _get_sync_session():
    """Returns a synchronous SQLAlchemy session for Celery tasks."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import os

    POSTGRES_USER = os.getenv("POSTGRES_USER", "appuser")
    POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "changeme123")
    POSTGRES_HOST = os.getenv("POSTGRES_HOST", "db")
    POSTGRES_PORT = os.getenv("POSTGRES_PORT", "5432")
    POSTGRES_DB = os.getenv("POSTGRES_DB", "scaffold_api")

    sync_url = (
        f"postgresql+psycopg2://{POSTGRES_USER}:{POSTGRES_PASSWORD}"
        f"@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
    )
    engine = create_engine(sync_url, pool_pre_ping=True)
    Session = sessionmaker(bind=engine)
    return Session()


@celery_app.task(name="app.tasks.subscription_tasks.process_expired_subscriptions", bind=True, max_retries=3)
def process_expired_subscriptions(self):
    """
    Daily job (00:05 UTC).

    Logic:
      past_due subs older than 3-day grace period → status='expired'
      User gets a new 'active' Free subscription (auto-downgrade, not hard block).

    Why auto-downgrade instead of hard block?
      → Reduces churn. Users can still log in, see their data, and re-subscribe.
      → Hard blocks create support tickets and refund requests.
    """
    from app.db.models import UserSubscription, SubscriptionPlan, AuditLog

    logger.info("[celery] process_expired_subscriptions starting")
    try:
        with _get_sync_session() as db:
            grace_cutoff = datetime.now(timezone.utc) - timedelta(days=3)

            # Find subscriptions past grace period
            expired_subs = db.execute(
                select(UserSubscription)
                .where(
                    UserSubscription.status == "past_due",
                    UserSubscription.current_period_end < grace_cutoff,
                )
            ).scalars().all()

            if not expired_subs:
                logger.info("[celery] No expired subscriptions to process")
                return {"processed": 0}

            # Fetch free plan
            free_plan = db.execute(
                select(SubscriptionPlan).where(SubscriptionPlan.name == "free")
            ).scalar_one_or_none()

            if not free_plan:
                logger.error("[celery] Free plan not found in DB — cannot downgrade users")
                return {"error": "free plan missing"}

            now = datetime.now(timezone.utc)
            processed = 0

            for sub in expired_subs:
                # 1. Mark old subscription as expired
                sub.status = "expired"

                # 2. Create new Free subscription for the user
                new_sub = UserSubscription(
                    user_id=sub.user_id,
                    plan_id=free_plan.id,
                    status="active",
                    billing_cycle="monthly",
                    current_period_start=now,
                    current_period_end=now + timedelta(days=30),
                )
                db.add(new_sub)

                # 3. Audit log
                audit = AuditLog(
                    user_id=sub.user_id,
                    event_type="plan_auto_downgraded",
                    entity_type="subscription",
                    entity_id=str(sub.id),
                    metadata_={
                        "reason": "past_due_grace_period_exceeded",
                        "old_plan_id": str(sub.plan_id),
                        "new_plan": "free",
                    },
                )
                db.add(audit)
                processed += 1

            db.commit()
            logger.info(f"[celery] process_expired_subscriptions: downgraded {processed} user(s) to Free plan")
            return {"processed": processed}

    except Exception as exc:
        logger.exception(f"[celery] process_expired_subscriptions failed: {exc}")
        raise self.retry(exc=exc, countdown=60 * 5)  # retry in 5 minutes


@celery_app.task(name="app.tasks.subscription_tasks.reset_monthly_counters", bind=True, max_retries=3)
def reset_monthly_counters(self):
    """
    Monthly job (1st of month, 00:05 UTC).

    Syncs final Redis usage counters into DB usage_counters table
    before Redis TTL naturally expires them.

    Redis counters expire after 35 days; this task ensures the DB
    has the authoritative count for billing reports and dispute resolution.
    """
    import redis as sync_redis
    import os
    from app.db.models import UsageCounter

    logger.info("[celery] reset_monthly_counters starting")
    try:
        REDIS_HOST     = os.getenv("REDIS_HOST", "redis")
        REDIS_PORT     = int(os.getenv("REDIS_PORT", "6379"))
        REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "changeme123")

        r = sync_redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True)

        # Previous month (we're on the 1st, so previous month is last month)
        now = datetime.now(timezone.utc)
        if now.month == 1:
            prev_year, prev_month = now.year - 1, 12
        else:
            prev_year, prev_month = now.year, now.month - 1

        # Scan Redis for all usage keys matching previous month
        pattern = f"usage:*:{prev_year}:{prev_month}"
        synced = 0

        with _get_sync_session() as db:
            for key in r.scan_iter(match=pattern):
                # Key format: usage:<user_id>:<year>:<month>
                parts = key.split(":")
                if len(parts) != 4:
                    continue
                _, user_id, year_str, month_str = parts
                count = int(r.get(key) or 0)

                # Upsert usage_counters
                existing = db.execute(
                    select(UsageCounter).where(
                        UsageCounter.user_id == user_id,
                        UsageCounter.period_year == int(year_str),
                        UsageCounter.period_month == int(month_str),
                    )
                ).scalar_one_or_none()

                if existing:
                    existing.total_requests = count
                    existing.updated_at = now
                else:
                    db.add(UsageCounter(
                        user_id=user_id,
                        period_year=int(year_str),
                        period_month=int(month_str),
                        total_requests=count,
                    ))
                synced += 1

            db.commit()

        logger.info(f"[celery] reset_monthly_counters: synced {synced} counter(s) for {prev_year}-{prev_month:02d}")
        return {"synced": synced, "period": f"{prev_year}-{prev_month:02d}"}

    except Exception as exc:
        logger.exception(f"[celery] reset_monthly_counters failed: {exc}")
        raise self.retry(exc=exc, countdown=60 * 10)


@celery_app.task(name="app.tasks.subscription_tasks.send_expiry_reminders")
def send_expiry_reminders():
    """
    Daily job: finds subscriptions expiring within 7 days and sends reminders.
    STUB — replace email body + provider with SendGrid/Resend/SES in production.
    """
    from app.db.models import UserSubscription, User

    logger.info("[celery] send_expiry_reminders starting")
    with _get_sync_session() as db:
        now = datetime.now(timezone.utc)
        warning_deadline = now + timedelta(days=7)

        expiring = db.execute(
            select(UserSubscription)
            .where(
                UserSubscription.status == "active",
                UserSubscription.current_period_end <= warning_deadline,
                UserSubscription.current_period_end > now,
            )
        ).scalars().all()

        for sub in expiring:
            user = db.get(User, sub.user_id)
            if user:
                days_left = (sub.current_period_end - now).days
                # TODO: replace with real email provider
                logger.info(
                    f"[email stub] Would send to {user.email}: "
                    f"subscription expires in {days_left} day(s)"
                )

    return {"reminders_queued": len(expiring)}
