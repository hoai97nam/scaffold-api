"""
Text-to-Speech (TTS) Router backed by CapCut TTS engine.

Endpoints:
  GET  /v1/tts/voices    → List available voices (API Key auth + rate limiting)
  POST /v1/tts/generate  → Generate audio speech (API Key auth + rate limiting + usage tracking)
  GET  /v1/tts/tasks/{task_id} → Check asynchronous task status
"""
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status

from app.core.dependencies import get_current_user_api_key, get_redis
from app.middleware.rate_limit import RateLimiter
from app.routers.users import _track_usage
from app.schemas.schemas import TTSGenerateRequest, TTSGenerateResponse, VoiceOut
from app.services.tts_service import tts_service
from app.services.capcut_tts_api.exceptions import CapCutError, CapCutTaskError

router = APIRouter(prefix="/v1/tts", tags=["Text-to-Speech (CapCut API)"])


@router.get(
    "/voices",
    response_model=list[VoiceOut],
    summary="List available TTS voices",
    description="**X-API-Key required.** Retrieves voices catalog from CapCut with voice_type and language filters.",
)
async def list_voices(
    lang: Optional[str] = Query(None, description="Filter by language code (e.g. 'vi-VN', 'en-US')"),
    auth_context: dict = Depends(get_current_user_api_key),
    redis=Depends(get_redis),
):
    # Rate limiting
    limiter = RateLimiter(redis)
    await limiter.check(auth_context["user_id"], auth_context["limits"])

    return tts_service.list_available_voices(lang=lang)


@router.post(
    "/generate",
    response_model=TTSGenerateResponse,
    summary="Synthesize speech from text",
    description=(
        "**X-API-Key required.** Full SaaS pipeline:\n\n"
        "1. Authenticates API Key\n"
        "2. Rate limiting & Monthly quota check\n"
        "3. Calls CapCut TTS generation engine (asynchronously off-thread)\n"
        "4. Returns audio URLs & task status\n"
        "5. Background usage tracking (Redis counter + PostgreSQL audit log)\n"
    ),
)
async def generate_speech(
    body: TTSGenerateRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    auth_context: dict = Depends(get_current_user_api_key),
    redis=Depends(get_redis),
):
    start_time = time.time()

    # 1. Rate limiting
    limiter = RateLimiter(redis)
    await limiter.check(auth_context["user_id"], auth_context["limits"])

    # 2. Monthly quota check
    QUOTA_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if current == false then current = 0 else current = tonumber(current) end
if current >= tonumber(ARGV[1]) then return -1 end
return tonumber(current)
"""
    now = datetime.now(timezone.utc)
    period_key = f"usage:{auth_context['user_id']}:{now.year}:{now.month}"
    count = await redis.eval(QUOTA_SCRIPT, 1, period_key, auth_context["limits"]["rpm_month"])
    if count == -1:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "MONTHLY_QUOTA_EXCEEDED",
                "limit": auth_context["limits"]["rpm_month"],
                "message": "Monthly API quota exceeded. Upgrade your plan for more requests.",
            },
        )

    # 3. Plan feature restrictions (e.g. Free plan text length restriction)
    if auth_context["plan"] == "free" and len(body.text) > 500:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FEATURE_RESTRICTED",
                "message": "Free tier is limited to 500 characters per request. Upgrade to Starter or Pro for long-form speech.",
            },
        )

    # 4. Invoke CapCut TTS service asynchronously without blocking event loop
    try:
        result = await tts_service.generate_speech_async(
            texts=body.text,
            voice=body.voice,
            rate=body.rate or "1.0",
            wait=bool(body.wait),
            timeout=30.0,
        )
    except CapCutTaskError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "TTS_TASK_FAILED", "message": str(exc)},
        )
    except CapCutError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "TTS_CLIENT_ERROR", "message": str(exc)},
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "TTS_INTERNAL_ERROR", "message": str(exc)},
        )

    elapsed_ms = int((time.time() - start_time) * 1000)

    # 5. Background usage tracking
    background_tasks.add_task(
        _track_usage,
        user_id=auth_context["user_id"],
        api_key_id=auth_context.get("api_key_id"),
        endpoint=request.url.path,
        method=request.method,
        status_code=200,
        response_time_ms=elapsed_ms,
        redis=redis,
    )

    return TTSGenerateResponse(
        ok=True,
        task_id=result.get("task_id"),
        status=result.get("status"),
        audio_urls=result.get("audio_urls") or [],
        token=result.get("token"),
        response_time_ms=elapsed_ms,
    )


@router.get(
    "/tasks/{task_id}",
    summary="Query status of an async TTS task",
    description="**X-API-Key required.** Checks the status and audio URL for an async TTS task ID.",
)
async def query_task(
    task_id: str,
    token: str = Query(..., description="Task token returned during initial submission"),
    auth_context: dict = Depends(get_current_user_api_key),
    redis=Depends(get_redis),
):
    limiter = RateLimiter(redis)
    await limiter.check(auth_context["user_id"], auth_context["limits"])

    try:
        res = await tts_service.query_task_async(task_id=task_id, token=token)
        return {"ok": True, "data": res}
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "TASK_QUERY_FAILED", "message": str(exc)},
        )
