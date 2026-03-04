"""
workers/tasks/budget.py — Budget Deduction Task (Phase 3.5)

Fire-and-forget task called after each successful inference.
Wraps the deduct_budget() logic from api/budget_guard.py
using a synchronous Redis client (Celery workers are sync).

The Celery task is intentionally thin — the actual Redis INCRBYFLOAT
logic and midnight-TTL calculation live in api/budget_guard.py so
there's a single authoritative implementation.
"""

import logging
import os

import redis as redis_sync

from workers.celery_app import celery_app

logger = logging.getLogger("tinai.tasks.budget")

_REDIS_URL             = os.environ.get("REDIS_URL_MAB", "redis://redis:6379/0")
_DEFAULT_BUDGET_CENTS  = float(os.environ.get("DEFAULT_DAILY_BUDGET_CENTS", "10000.0"))


@celery_app.task(name="workers.tasks.budget.deduct_budget", bind=True, max_retries=3)
def deduct_budget(self, client_key: str, cost_cents: float) -> None:
    """
    Atomically deduct cost_cents from the client's daily Redis budget.

    Args:
        client_key:  SHA-256 derived key from auth.verify_api_key.
        cost_cents:  Cost of the completed inference in USD cents.
    """
    from api.redis_keys import key_daily_budget, key_budget_blocked
    import math
    from datetime import datetime, timezone, timedelta

    r = redis_sync.from_url(_REDIS_URL, decode_responses=True)
    try:
        daily_key   = key_daily_budget(client_key)
        blocked_key = key_budget_blocked(client_key)

        new_total = r.incrbyfloat(daily_key, cost_cents)

        # Set TTL to midnight UTC only if key was just created
        ttl = r.ttl(daily_key)
        if ttl == -1:
            now_utc         = datetime.now(timezone.utc)
            midnight_utc    = (now_utc + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            seconds_to_midnight = math.ceil((midnight_utc - now_utc).total_seconds())
            r.expire(daily_key, seconds_to_midnight)

        # If over limit, set the block flag
        if new_total >= _DEFAULT_BUDGET_CENTS:
            ttl = r.ttl(daily_key)
            r.set(blocked_key, "1", ex=max(ttl, 1))
            logger.warning("Client %s budget EXHAUSTED (%.4f cents)", client_key, new_total)
        else:
            logger.debug("Budget deducted: client=%s cost=%.4f total=%.4f", client_key, cost_cents, new_total)

    except Exception as exc:
        logger.error("Budget deduction failed for %s: %s — retrying", client_key, exc)
        raise self.retry(exc=exc, countdown=2 ** self.request.retries)
    finally:
        r.close()
