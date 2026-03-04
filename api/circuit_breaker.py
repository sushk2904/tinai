"""
api/circuit_breaker.py — Per-Provider Circuit Breaker (Phase 2.7)

Protects the system from cascading failures when a provider is degraded.
A failed provider's circuit trips to OPEN after threshold failures, stopping
traffic to it until a half-open probe succeeds.

States (stored in Redis DB 0):
  CLOSED    → normal operation, traffic flows
  OPEN      → provider is down, traffic is diverted to fallback
  HALF_OPEN → one probe request allowed to check recovery

Redis keys:
  circuit:{provider}:state    → "CLOSED" | "OPEN" | "HALF_OPEN"
  circuit:{provider}:failures → rolling failure counter (TTL = window_seconds)

Read path  (sync — called in request loop):
  is_open(provider, redis) → bool

Write path (async — called as Celery tasks, NOT inline):
  record_failure(provider, redis) — increment failures, trip to OPEN if threshold hit
  record_success(provider, redis) — reset failures, close circuit

These write functions are designed to be called from:
  workers/tasks/telemetry.py (Phase 3.2) as Celery tasks.
"""

import logging

from redis.asyncio import Redis

from api.redis_keys import (
    CIRCUIT_CLOSED, CIRCUIT_OPEN, CIRCUIT_HALF_OPEN,
    key_circuit_state, key_circuit_failures,
)

logger = logging.getLogger("tinai.circuit_breaker")

# Number of failures within the window before tripping to OPEN.
FAILURE_THRESHOLD: int  = 5
# Rolling window in seconds for failure counter TTL.
FAILURE_WINDOW_S:  int  = 60
# Initial state for all providers.
_INITIAL_STATE: str     = CIRCUIT_CLOSED


async def is_open(provider: str, redis: Redis) -> bool:
    """
    Check if a provider's circuit breaker is in the OPEN state.

    Called synchronously in the request loop — single Redis GET, O(1).
    If the circuit state key is missing, defaults to CLOSED (fail-open).

    Args:
        provider: Provider name (validated by redis_keys._assert_provider).
        redis:    Shared Redis client (read-only in sync path).

    Returns:
        True if circuit is OPEN (provider should be skipped).
        False if CLOSED or HALF_OPEN (traffic allowed).
    """
    state = await redis.get(key_circuit_state(provider))
    result = state == CIRCUIT_OPEN
    if result:
        logger.info("Circuit OPEN for %s — skipping provider.", provider)
    return result


async def record_failure(provider: str, redis: Redis) -> None:
    """
    Record a provider failure.

    MUST be called from a Celery background task — NOT inline in the request loop.

    Atomically increments the rolling failure counter.
    Trips the circuit to OPEN if the failure threshold is breached.
    Sets TTL on the failure key so the counter auto-resets after the window.

    Also handles HALF_OPEN → OPEN transition: if the probe request failed,
    the circuit stays open.
    """
    failure_key = key_circuit_failures(provider)
    state_key   = key_circuit_state(provider)

    count = await redis.incr(failure_key)

    # Set TTL on first failure in the window (so the counter auto-resets)
    if count == 1:
        await redis.expire(failure_key, FAILURE_WINDOW_S)

    current_state = await redis.get(state_key)

    if count >= FAILURE_THRESHOLD or current_state == CIRCUIT_HALF_OPEN:
        # Trip to OPEN
        await redis.set(state_key, CIRCUIT_OPEN)
        logger.warning(
            "Circuit TRIPPED for %s (failures=%d threshold=%d)",
            provider, count, FAILURE_THRESHOLD,
        )
    elif current_state is None:
        # Initialise state key if missing
        await redis.set(state_key, CIRCUIT_CLOSED)


async def record_success(provider: str, redis: Redis) -> None:
    """
    Record a successful provider call.

    MUST be called from a Celery background task — NOT inline in the request loop.

    Resets the failure counter and transitions the circuit back to CLOSED.
    After a HALF_OPEN probe succeeds, this closes the circuit permanently
    until the next failure threshold breach.
    """
    await redis.delete(key_circuit_failures(provider))
    await redis.set(key_circuit_state(provider), CIRCUIT_CLOSED)
    logger.info("Circuit CLOSED for %s (success recorded).", provider)
