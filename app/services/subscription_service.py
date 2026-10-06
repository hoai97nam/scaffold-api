import logging
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException
from datetime import datetime, timezone, timedelta

# In a real app you would import your CRUD functions and external SDKs
# from app.crud import subscription_crud, user_crud
# from app.external import stripe_client
# from app.tasks.subscription_tasks import send_upgrade_email

logger = logging.getLogger(__name__)

class SubscriptionService:
    """
    Core business logic for handling user subscriptions.
    Notice how this class orchestrates multiple operations: DB, External APIs, and Async Tasks.
    """

    async def upgrade_user_plan(self, db: AsyncSession, user_id: str, new_plan_id: str):
        logger.info(f"Initiating plan upgrade for user {user_id} to plan {new_plan_id}")

        # 1. Database Read (via CRUD)
        # current_sub = await subscription_crud.get_active_subscription(db, user_id)
        # new_plan = await subscription_crud.get_plan(db, new_plan_id)
        
        # --- Mocking the DB data for this sketch ---
        current_sub = {"plan_id": "old_plan", "status": "active"}
        new_plan = {"price_monthly": 19.00, "name": "starter"}

        # 2. Business Rule Validations
        if current_sub and current_sub["plan_id"] == new_plan_id:
            raise HTTPException(status_code=400, detail="User is already on this plan.")
        
        if new_plan["price_monthly"] > 0:
            # 3. External API Call (e.g. Stripe)
            try:
                logger.info(f"Charging Stripe for ${new_plan['price_monthly']}...")
                # payment_intent = await stripe_client.charge_customer(...)
            except Exception as e:
                logger.error(f"Payment failed: {str(e)}")
                raise HTTPException(status_code=402, detail="Payment failed. Please check your card.")

        # 4. Database Write (via CRUD)
        try:
            logger.info("Updating subscription record in database...")
            # await subscription_crud.update_user_subscription(
            #     db, 
            #     user_id=user_id, 
            #     plan_id=new_plan_id, 
            #     period_start=datetime.now(timezone.utc),
            #     period_end=datetime.now(timezone.utc) + timedelta(days=30)
            # )
            # await db.commit()
        except Exception as e:
            # await db.rollback()
            raise HTTPException(status_code=500, detail="Failed to update subscription in database.")

        # 5. Background Task Dispatch (Celery)
        logger.info("Dispatching async receipt email...")
        # send_upgrade_email.delay(user_id=user_id, plan_name=new_plan["name"])

        return {"status": "success", "message": f"Successfully upgraded to {new_plan['name']}"}

# Expose a singleton instance of the service
subscription_service = SubscriptionService()
