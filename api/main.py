"""
api/main.py — FastAPI Application Entry Point (Phase 2.1)

Controls the full lifecycle of the TINAI engine:
  • Startup  → opens Redis connection pool + asyncpg connection pool
  • Runtime  → registers routers (added phase by phase per TODO)
  • Shutdown → drains and closes both pools cleanly

Architecture invariants enforced here:
  tradeoffs-info §1 — The sync request loop may ONLY:
    1. Read MAB weights from Redis
    2. Call the external LLM provider
    3. Return the response to the client
  Nothing else. All other work is offloaded to Celery tasks.

Pool design:
  Redis  → single shared redis.asyncio.Redis client backed by a
           ConnectionPool. The pool is created once at startup and reused
           across all concurrent Uvicorn workers sharing this process.
  asyncpg → asyncpg.Pool with min/max sizes from settings. asyncpg pools
           are safe to share across asyncio tasks in the same event loop.
"""

import logging
import uuid
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Request, Response
from redis.asyncio import ConnectionPool, Redis

from api.config import get_settings

logger = logging.getLogger("tinai.api")

settings = get_settings()


# ===========================================================================
# Lifespan — pool creation at startup, graceful teardown at shutdown
# ===========================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan context manager.

    Everything before `yield` runs at startup (before the first request).
    Everything after `yield` runs at shutdown (after the last request).

    Storing pools on `app.state` makes them available to dependency injectors
    in api/dependencies.py via `request.app.state.*` — the standard FastAPI
    pattern for sharing resources without global variables.
    """

    # -----------------------------------------------------------------------
    # STARTUP
    # -----------------------------------------------------------------------
    logger.info("TINAI Engine booting — opening connection pools…")

    # --- Redis (Control Plane — DB 0) ---------------------------------------
    # ConnectionPool is created once; the Redis client wraps it.
    # decode_responses=True: all Redis values come back as Python str,
    # not bytes — avoids .decode() calls throughout the codebase.
    # max_connections=50: headroom for 4 Uvicorn workers × concurrent requests.
    redis_pool = ConnectionPool.from_url(
        settings.redis_url_mab,
        decode_responses=True,
        max_connections=50,
    )
    app.state.redis = Redis(connection_pool=redis_pool)

    # Verify Redis is reachable before accepting traffic. A failure here
    # causes the container to exit non-zero, which Docker Compose healthcheck
    # will catch and restart the container rather than serve 500s silently.
    await app.state.redis.ping()
    logger.info("Redis pool ready (DB 0 — MAB/cache/circuit)")

    # --- asyncpg Connection Pool (Postgres) ---------------------------------
    # asyncpg.create_pool() opens `min_size` connections immediately.
    # Additional connections are created on demand up to `max_size`.
    # `command_timeout=30.0` prevents a hung Postgres query from blocking a
    # Uvicorn worker indefinitely.
    #
    # The DATABASE_URL uses the postgresql+asyncpg:// format from .env.
    # asyncpg.create_pool() only accepts raw postgresql:// — strip the prefix.
    raw_dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")

    app.state.pool = await asyncpg.create_pool(
        dsn=raw_dsn,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        command_timeout=30.0,
    )
    logger.info(
        "asyncpg pool ready (min=%d, max=%d)",
        settings.db_pool_min_size,
        settings.db_pool_max_size,
    )

    logger.info("TINAI Engine online — ready to accept requests.")

    # -----------------------------------------------------------------------
    # RUNTIME — yield hands control to FastAPI
    # -----------------------------------------------------------------------
    yield

    # -----------------------------------------------------------------------
    # SHUTDOWN
    # -----------------------------------------------------------------------
    logger.info("TINAI Engine shutting down — draining pools…")

    # Close Redis pool — waits for all in-flight commands to complete.
    await app.state.redis.aclose()
    await redis_pool.aclose()
    logger.info("Redis pool closed.")

    # Close asyncpg pool — waits for all connections to be returned and closed.
    await app.state.pool.close()
    logger.info("asyncpg pool closed.")

    logger.info("TINAI Engine offline.")


# ===========================================================================
# FastAPI Application
# ===========================================================================

app = FastAPI(
    title="TINAI Execution Layer",
    description=(
        "Adaptive AI Routing & Reliability Platform. "
        "Control Plane: Redis-backed MAB routing. "
        "Data Plane: sub-1500ms LLM provider calls. "
        "Reliability: Celery async workers for all non-blocking work."
    ),
    version="1.0.0",
    # Disable /docs and /redoc in prod — reduces attack surface.
    docs_url="/docs" if settings.environment == "dev" else None,
    redoc_url="/redoc" if settings.environment == "dev" else None,
    lifespan=lifespan,
)


# ===========================================================================
# Middleware — Security & Tracing
# ===========================================================================

# 1. CORS Lockdown (Middleware)
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)

@app.middleware("http")
async def security_headers_middleware(request: Request, call_next) -> Response:
    """
    Injects standard security headers to protect against common web vulnerabilities.
    """
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    # HSTS: 1 year (only if using HTTPS, but good practice to include)
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

@app.middleware("http")
async def inject_request_id(request: Request, call_next) -> Response:
    """
    Injects a UUID request ID into every request/response cycle.
    Phase 8.1: Parity between sync path and background tasks.
    """
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id

    response = await call_next(request)
    # Ensure X-Request-ID is present in the final response
    response.headers["X-Request-ID"] = request_id
    return response


# ===========================================================================
# Core Routes
# ===========================================================================

@app.get(
    "/health",
    tags=["Infrastructure"],
    summary="Liveness probe",
    response_description="Returns 200 if the engine is accepting requests.",
)
async def health_check(request: Request):
    """
    Liveness endpoint polled by Docker Compose healthcheck and load balancers.

    Checks that both the Redis client and asyncpg pool are still connected —
    a pool that silently lost its connections returns 'degraded' so the
    orchestrator can restart the container.
    """
    redis_ok = False
    db_ok    = False

    try:
        redis_ok = await request.app.state.redis.ping()
    except Exception:
        pass

    try:
        async with request.app.state.pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
            db_ok = True
    except Exception:
        pass

    overall = "healthy" if (redis_ok and db_ok) else "degraded"

    return {
        "status":      overall,
        "service":     "TINAI API",
        "version":     "1.0.0",
        "environment": settings.environment,
        "pools": {
            "redis": "ok" if redis_ok else "error",
            "postgres": "ok" if db_ok else "error",
        },
    }


@app.get(
    "/",
    tags=["Infrastructure"],
    summary="Root — confirms control plane is live.",
    include_in_schema=False,
)
async def root():
    return {
        "message":     "TINAI Control Plane is live.",
        "docs":        "/docs" if settings.environment == "dev" else "disabled in prod",
        "phase":       "2.1 — Configuration & Lifecycle",
    }


# ===========================================================================
# Router registration (uncomment as each phase is implemented)
# ===========================================================================

# Phase 2.3 — Import providers package at startup.
# If any provider file has a syntax error, Uvicorn will fail to start here
# and /health will not respond — making this the implicit import test.
from api.providers import PROVIDER_MAP  # noqa: E402
from api.providers.base import ProviderResponse, calculate_cost_cents  # noqa: E402
from api.providers.retry import provider_retry  # noqa: E402

logger.info("Provider map loaded: %s", list(PROVIDER_MAP.keys()))


@app.get(
    "/v1/providers",
    tags=["Diagnostics"],
    summary="List registered providers and their status.",
)
async def list_providers():
    """
    Diagnostic endpoint — returns the registered PROVIDER_MAP keys and
    confirms the provider abstraction layer loaded correctly.
    Safe to call in dev. Disabled automatically in prod via settings.
    """
    if settings.environment != "dev":
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Not found.")
    return {
        "providers": list(PROVIDER_MAP.keys()),
        "phase":     "2.3 — Provider Abstraction Layer",
        "note":      "All providers target external APIs — no local models (tradeoffs-info §5).",
    }


# Phase 2.6 — Inference endpoint (ACTIVE)
from api.routers.infer import router as infer_router  # noqa: E402
app.include_router(infer_router, prefix="/v1", tags=["Inference"])

# Phase 4.2 / 4.3 — Admin endpoints (chaos + load shed)
from api.routers.admin import router as admin_router
app.include_router(admin_router)