"""
Celery application factory.

Broker + Backend: Redis (same instance as rate-limiting cache).
Two queues:
  - default  : general async tasks (usage logging, emails)
  - periodic : Celery Beat scheduled jobs

Beat schedule (runs automatically in celery_beat container):
  - process_expired_subscriptions : daily at 00:05 UTC
  - reset_monthly_counters        : 1st of each month at 00:05 UTC
"""
import os
from celery import Celery
from celery.schedules import crontab

REDIS_HOST     = os.getenv("REDIS_HOST", "redis")
REDIS_PORT     = os.getenv("REDIS_PORT", "6379")
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "changeme123")
BROKER_URL     = f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/0"

celery_app = Celery(
    "scaffold_api",
    broker=BROKER_URL,
    backend=BROKER_URL,
    include=[
        "app.tasks.usage_tasks",
        "app.tasks.subscription_tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_routes={
        "app.tasks.usage_tasks.*":        {"queue": "default"},
        "app.tasks.subscription_tasks.*": {"queue": "default"},
    },
    # Celery Beat periodic schedule
    beat_schedule={
        # Runs daily at 00:05 UTC — checks for past_due subs past grace period
        "process-expired-subscriptions-daily": {
            "task": "app.tasks.subscription_tasks.process_expired_subscriptions",
            "schedule": crontab(hour=0, minute=5),
        },
        # Runs on 1st of each month at 00:05 UTC — syncs Redis counters to DB
        "reset-monthly-counters": {
            "task": "app.tasks.subscription_tasks.reset_monthly_counters",
            "schedule": crontab(hour=0, minute=5, day_of_month=1),
        },
    },
)
