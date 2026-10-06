from .celery_app import celery_app

@celery_app.task
def record_usage_log(user_id, api_key_id, endpoint, method, status_code, period_year, period_month):
    pass
