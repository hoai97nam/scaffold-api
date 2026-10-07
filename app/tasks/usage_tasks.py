"""
Usage Celery tasks.

record_usage_log()
  Called from API route background tasks after every successful request.
  Writes to PostgreSQL usage_logs for the audit trail.
  The fast Redis counter is already incremented synchronously in the route handler;
  this task only handles the slower DB write.

  Why Celery instead of FastAPI BackgroundTasks?
    FastAPI BackgroundTasks run in the same process, still consuming server memory.
    Celery offloads the work entirely to the worker container, keeping the API
    process lean and fast under high throughput.
"""
import logging
from datetime import datetime, timezone

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _get_sync_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import os

    sync_url = (
        "postgresql+psycopg2://"
        f"{os.getenv('POSTGRES_USER','appuser')}:{os.getenv('POSTGRES_PASSWORD','changeme123')}"
        f"@{os.getenv('POSTGRES_HOST','db')}:{os.getenv('POSTGRES_PORT','5432')}"
        f"/{os.getenv('POSTGRES_DB','scaffold_api')}"
    )
    engine = create_engine(sync_url, pool_pre_ping=True)
    return sessionmaker(bind=engine)()


@celery_app.task(
    name="app.tasks.usage_tasks.record_usage_log",
    bind=True,
    max_retries=5,
    default_retry_delay=10,   # retry in 10s if DB is temporarily unavailable
)
def record_usage_log(
    self,
    user_id: str,
    api_key_id: str | None,
    endpoint: str,
    method: str,
    status_code: int,
    response_time_ms: int,
    period_year: int,
    period_month: int,
):
    """
    Async DB write of a single API request log entry.
    Also updates api_keys.last_used_at.

    Failure handling:
      - Retried up to 5 times with 10s delay (handles transient DB errors)
      - If all retries fail, log is lost (acceptable: Redis counter is authoritative)
    """
    from app.db.models import UsageLog, APIKey
    from sqlalchemy import update

    try:
        with _get_sync_session() as db:
            log = UsageLog(
                user_id=user_id,
                api_key_id=api_key_id,
                endpoint=endpoint,
                method=method,
                status_code=status_code,
                response_time_ms=response_time_ms,
                period_year=period_year,
                period_month=period_month,
                created_at=datetime.now(timezone.utc),
            )
            db.add(log)

            # Update last_used_at on the API key (best-effort)
            if api_key_id:
                db.execute(
                    update(APIKey)
                    .where(APIKey.id == api_key_id)
                    .values(last_used_at=datetime.now(timezone.utc))
                )

            db.commit()

    except Exception as exc:
        logger.warning(f"[celery] record_usage_log failed (attempt {self.request.retries + 1}): {exc}")
        raise self.retry(exc=exc)
