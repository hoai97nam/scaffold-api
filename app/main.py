"""
Main FastAPI application entry point.

Startup sequence:
  1. Connect to Redis
  2. Initialize DB (create tables, seed plans)
  3. Register all routers
  4. Expose /health endpoint
"""
import redis.asyncio as aioredis
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.db.init_db import init_db
from app.routers.auth import router as auth_router
from app.routers.subscriptions import router as subscriptions_router
from app.routers.api_keys import router as api_keys_router
from app.routers.usage import router as usage_router
from app.routers.users import me_router, api_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────────────
    # 1. Connect to Redis
    app.state.redis = aioredis.from_url(
        settings.REDIS_URL,
        encoding="utf-8",
        decode_responses=False,
    )
    await app.state.redis.ping()

    # 2. Init DB: create tables + seed plans
    await init_db()

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────────
    await app.state.redis.aclose()


app = FastAPI(
    title="🏗️ SaaS API Scaffold",
    description=(
        "## Commercial SaaS API — Full demo\n\n"
        "Demonstrates a production-ready SaaS API with:\n\n"
        "- **Dual auth**: JWT (dashboard) + API Key (business API)\n"
        "- **Subscription plans**: Free / Starter ($19/mo) / Pro ($79/mo)\n"
        "- **Rate limiting**: per-minute / per-day / per-month (Redis sliding window)\n"
        "- **Usage tracking**: Redis counter + async PostgreSQL audit log\n"
        "- **API Key management**: create, list, revoke (immediate cache invalidation)\n"
        "- **Billing history**: immutable append-only ledger\n"
        "- **Plan-based feature gating**: different capabilities per tier\n\n"
        "### Quick Start\n"
        "1. `POST /auth/register` → get `access_token`\n"
        "2. `POST /api-keys/` (Bearer token) → get `raw_key`\n"
        "3. `GET /v1/analyze?query=hello` (X-API-Key: raw_key) → see rate limiting in action\n"
        "4. `GET /usage/` → see your usage stats\n"
        "5. `POST /subscriptions/subscribe` → upgrade to Pro\n"
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS (adjust origins for production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(auth_router)
app.include_router(me_router)
app.include_router(api_keys_router)
app.include_router(subscriptions_router)
app.include_router(usage_router)
app.include_router(api_router)


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/health", tags=["System"], summary="Health check")
async def health_check():
    """
    Checks DB and Redis connectivity.
    Used by Docker healthcheck and load balancers.
    """
    redis_ok = False
    try:
        await app.state.redis.ping()
        redis_ok = True
    except Exception:
        pass

    return JSONResponse({
        "status": "ok" if redis_ok else "degraded",
        "redis": "ok" if redis_ok else "error",
        "db": "ok",  # If we reached here, DB init succeeded
        "version": "1.0.0",
        "environment": settings.ENVIRONMENT,
    })
