import asyncio
import logging
from app.db.session import engine, Base
from app.db.models import User, SubscriptionPlan, UserSubscription, APIKey

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def init_db():
    logger.info("Creating database tables...")
    async with engine.begin() as conn:
        # Warning: This will drop all tables and recreate them. 
        # For production, use Alembic migrations instead!
        # await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables created successfully!")

if __name__ == "__main__":
    asyncio.run(init_db())
