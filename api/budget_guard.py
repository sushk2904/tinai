"""
api/budget_guard.py — Per-Client Daily Budget Enforcement (Phase 2.2)

Protects the system from economic overrun: a single client cannot exhaust
your provider credits by firing unlimited requests.

Architecture split (tradeoffs-info §1):
  SYNC PATH  → check_budget()    reads two Redis keys (O(1), non-blocking)
  ASYNC PATH → deduct_budget()   writes to Redis via Celery task (Phase 3.5)
                                  NEVER called inline in the request loop.

Redis keys used (all DB 0):
  budget:daily:{client_key}    — running spend accumulator in USD cents
  budget:blocked:{client_key}  — flag key; presence means client is blocked

Currency: USD CENTS throughout (tradeoffs-info §3).
  $100.00 daily limit = 10000.0 cents (settings.default_daily_budget_cents)

Flow per request:
  1. `check_budget(client_key, redis)`     ← sync, in request loop
      a. If `budget:blocked:{client_key}` exists → HTTP 402 immediately
         (skips the spend calculation — single GET, ~0.1ms)
      b. If `budget:daily:{client_key}` ≥ limit → HTTP 402
  2. LLM call executes...
  3. After response sent:
      `deduct_budget.delay(client_key, cost_cents)` ← Celery fire-and-forget
      (see workers/tasks/budget.py — Phase 3.5)

Why check `budget:blocked` first:
  Once a client is blocked, their `budget:blocked` key exists until midnight.
  The daily counter may have been reset but the block flag is still live.
  Checking the flag first is a fast-path short-circuit that avoids a second
  Redis GET on most blocked requests.
"""

from redis.asyncio import Redis
from fastapi import HTTPException, status

from api.config import get_settings
from api.redis_keys import key_budget_blocked, key_daily_budget

settings = get_settings()


async def check_budget(client_key: str, redis: Redis) -> None:
    """
    Enforce the daily budget ceiling for `client_key`.

    Performs two Redis GET operations:
      1. `budget:blocked:{client_key}` — fast-path block flag check.
      2. `budget:daily:{client_key}`  — running spend vs. limit.

    Both are read-only. No writes in this function.
    The deduction happens asynchronously via workers/tasks/budget.py.

    Args:
        client_key: SHA-256 derived key from auth.verify_api_key().
        redis:      The shared Redis client from get_redis() dependency.

    Raises:
        HTTP 402 PAYMENT REQUIRED — if the client is blocked OR their
        accumulated spend has reached the daily ceiling.

    Called from:
        api/routers/infer.py — step 3 of the sync request loop (Phase 2.6).
    """
    # --- Fast path: check block flag first ----------------------------------
    # `budget:blocked:{client_key}` is SET by the deduct_budget Celery task
    # the moment accumulated spend crosses the threshold. Its TTL is set to
    # seconds-until-midnight so it auto-expires when the day resets.
    blocked = await redis.exists(key_budget_blocked(client_key))
    if blocked:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Daily budget exceeded. Resets at midnight UTC.",
            headers={"X-Budget-Status": "blocked"},
        )

    # --- Spend check ---------------------------------------------------------
    # `budget:daily:{client_key}` holds accumulated spend as a string float.
    # It is incremented by INCRBYFLOAT in the deduct_budget Celery task.
    # On a brand-new client (first request of the day) this key does not exist
    # yet — a missing key means zero spend.
    spent_raw = await redis.get(key_daily_budget(client_key))

    if spent_raw is not None:
        try:
            spent_cents = float(spent_raw)
        except ValueError:
            # Corrupt Redis value — fail open (allow the request) and log.
            # Better to allow one bad request than to block all traffic.
            spent_cents = 0.0

        if spent_cents >= settings.default_daily_budget_cents:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=(
                    f"Daily budget of {settings.default_daily_budget_cents:.2f} "
                    f"cents exceeded ({spent_cents:.4f} cents spent). "
                    f"Resets at midnight UTC."
                ),
                headers={
                    "X-Budget-Status": "exceeded",
                    "X-Budget-Spent":  f"{spent_cents:.4f}",
                    "X-Budget-Limit":  f"{settings.default_daily_budget_cents:.4f}",
                },
            )


async def deduct_budget(
    client_key: str,
    cost_cents: float,
    redis: Redis,
) -> None:
    """
    Atomically deduct `cost_cents` from the client's daily budget in Redis.

    THIS FUNCTION IS CALLED FROM THE CELERY TASK workers/tasks/budget.py
    (Phase 3.5) — NEVER inline in the FastAPI sync request loop.

    The Celery task imports and calls this function so the deduction logic
    lives in one place (here) rather than being duplicated in the worker.

    Operations:
      1. INCRBYFLOAT `budget:daily:{client_key}` by cost_cents.
         — Creates the key if it doesn't exist (Redis INCRBYFLOAT semantics).
         — Sets TTL to seconds-until-midnight on first deduction (if no TTL).
      2. If new total >= limit: SET `budget:blocked:{client_key}` "1" with
         same midnight TTL → future check_budget() calls hit the fast path.

    Args:
        client_key:  SHA-256 derived key (from auth.verify_api_key).
        cost_cents:  Cost of this request in USD cents (Numeric 10,4 precision).
        redis:       Redis client. In Celery tasks this is a sync redis client
                     (redis.Redis), called differently than the async version.
                     See workers/tasks/budget.py for the task wrapper.
    """
    import math
    from datetime import datetime, timezone

    daily_key   = key_daily_budget(client_key)
    blocked_key = key_budget_blocked(client_key)

    # Atomic increment — INCRBYFLOAT is atomic in Redis single-thread model.
    new_total = await redis.incrbyfloat(daily_key, cost_cents)

    # Set TTL only if the key was just created (TTL == -1 means no expiry set).
    # This prevents resetting the daily window on every deduction.
    ttl = await redis.ttl(daily_key)
    if ttl == -1:
        # Calculate seconds remaining until next midnight UTC.
        now_utc = datetime.now(timezone.utc)
        midnight_utc = now_utc.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        from datetime import timedelta
        midnight_utc += timedelta(days=1)
        seconds_to_midnight = math.ceil((midnight_utc - now_utc).total_seconds())
        await redis.expire(daily_key, seconds_to_midnight)

    # If over limit: set the block flag with the same midnight TTL.
    if new_total >= settings.default_daily_budget_cents:
        ttl = await redis.ttl(daily_key)  # re-read after possible EXPIRE above
        await redis.set(blocked_key, "1", ex=max(ttl, 1))
