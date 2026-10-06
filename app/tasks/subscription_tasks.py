from .celery_app import celery_app

@celery_app.task
def process_expired_subscriptions():
    pass
