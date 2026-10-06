from fastapi import Request, HTTPException
from app.core.api_keys import hash_api_key

async def api_key_auth(request: Request):
    raw_key = request.headers.get("X-API-Key")
    if not raw_key:
        raise HTTPException(status_code=401, detail="API key required")
    # Add full DB and cache checking here
    return {"user_id": "placeholder"}
