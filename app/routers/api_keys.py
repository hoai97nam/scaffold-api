from fastapi import APIRouter

router = APIRouter(prefix="/api-keys", tags=["API Keys"])

@router.get("/")
async def list_api_keys():
    pass

@router.post("/")
async def create_api_key():
    pass

@router.delete("/{key_id}")
async def revoke_api_key(key_id: str):
    pass
