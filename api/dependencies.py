"""
api/dependencies.py — FastAPI Dependency Injectors (Phase 2.1)

Provides `get_redis()` and `get_db()` — the two injectors that give routes
access to the Redis and Postgres pools opened during the lifespan.

Design decisions:
  • Both functions are async generators using `yield` so FastAPI's dependency
    system can guarantee cleanup (connection release) even if a route raises.
  • `get_redis()` yields the shared `redis.asyncio.Redis` client directly —
    the client itself is thread-safe and pool-managed; no per-request teardown needed.
  • `get_db()` acquires a connection from the asyncpg pool and releases it
    back to the pool in the `finally` block — even on errors or timeouts.
  • Both access `request.app.state.*` — the pools are stored there by the
    lifespan context manager in main.py.
  • `Annotated` type aliases (`RedisDep`, `DbDep`) give routes a clean,
    readable signature instead of `Depends(get_redis)` boilerplate everywhere.

Usage in a route:
    from api.dependencies import RedisDep, DbDep

    @router.post("/v1/infer")
    async def infer(redis: RedisDep, db: DbDep, ...):
        weight = await redis.get(key_mab_weights("groq"))
        row = await db.fetchrow("SELECT * FROM inference_logs LIMIT 1")
"""

from typing import Annotated, AsyncGenerator

import asyncpg
from fastapi import Depends, Request
from redis.asyncio import Redis


# ---------------------------------------------------------------------------
# Redis injector (Control Plane — DB 0 only)
# tradeoffs-info §1: the sync loop may ONLY read from Redis (MAB weights,
# L1 cache check, rate-limit, circuit-breaker state).
# Writes are done in Celery tasks, not here.
# ---------------------------------------------------------------------------

async def get_redis(request: Request) -> AsyncGenerator[Redis, None]:
    """
    Yields the application-scoped Redis client from app.state.
    The client is backed by a ConnectionPool created during lifespan startup
    (see main.py). FastAPI reuses this pool across all requests — no new
    connections are created per request.

    The `yield` (rather than a plain `return`) allows FastAPI's dependency
    system to handle any cleanup if we ever need post-request teardown here.
    """
    yield request.app.state.redis


# ---------------------------------------------------------------------------
# Postgres injector (for background / admin reads — NOT for writes)
# tradeoffs-info §1 cross-cutting invariant:
#   "No Postgres WRITE in the sync request loop."
# This injector provides read-only access for routes that need to query
# inference history or check budgets in the DB directly.
# All WRITES go through Celery tasks exclusively.
# ---------------------------------------------------------------------------

async def get_db(request: Request) -> AsyncGenerator[asyncpg.Connection, None]:
    """
    Acquires a connection from the asyncpg pool stored on app.state.pool,
    yields it to the route, then releases it back to the pool.

    Using `async with pool.acquire()` in a finally block guarantees the
    connection is returned even if:
      - The route raises an HTTPException
      - The LLM provider times out
      - An unhandled exception occurs

    Connection is NOT committed here — that's the caller's responsibility
    (or Celery's for writes).
    """
    async with request.app.state.pool.acquire() as conn:
        yield conn


# ---------------------------------------------------------------------------
# Annotated type aliases — clean dependency injection at call sites
#
# Instead of:
#   async def my_route(redis: Redis = Depends(get_redis), db: asyncpg.Connection = Depends(get_db)):
#
# Routes can write:
#   async def my_route(redis: RedisDep, db: DbDep):
# ---------------------------------------------------------------------------

RedisDep = Annotated[Redis, Depends(get_redis)]
DbDep    = Annotated[asyncpg.Connection, Depends(get_db)]
