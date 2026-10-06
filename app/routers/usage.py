from fastapi import APIRouter

router = APIRouter(prefix="/usage", tags=["Usage"])

@router.get("/")
async def get_usage():
    pass
