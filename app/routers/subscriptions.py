from fastapi import APIRouter

router = APIRouter(prefix="/subscriptions", tags=["Subscriptions"])

@router.get("/plans")
async def list_plans():
    pass

@router.post("/subscribe")
async def subscribe():
    pass
