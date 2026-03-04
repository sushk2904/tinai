"""
api/chaos.py — Failure & Chaos Engine (Phase 4.2)

Dynamically injects synthetic failures into provider calls based on Redis flags.
Allows testing the Circuit Breaker and MAB routing adaptation in production-like
failure scenarios without actually burning provider quotas.

Supported modes:
  - "none":       No effect (default).
  - "slow":       Injects 1.0 - 3.0s latency (gray failure).
  - "timeout":    Injects latency exceeding the SLA then raises TimeoutException.
  - "rate_limit": Immediately raises HTTPStatusError(429).
"""

import asyncio
import logging
import random
from enum import Enum

import httpx

from api.redis_keys import key_chaos_mode

logger = logging.getLogger("tinai.chaos")


class ChaosMode(str, Enum):
    NONE = "none"
    SLOW = "slow"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"


async def inject_chaos(provider: str, redis) -> None:
    """
    Checks Redis for a chaos flag and injects the specified failure mode.
    Called inside the provider abstraction layer, immediately BEFORE the HTTP request.

    Args:
        provider: The provider name (e.g. "groq", "openrouter").
        redis:    The redis-py async client.

    Raises:
        httpx.TimeoutException: If in 'timeout' mode.
        httpx.HTTPStatusError:  If in 'rate_limit' mode (synthetic 429).
    """
    mode_str = await redis.get(key_chaos_mode(provider))
    
    if not mode_str or mode_str == ChaosMode.NONE.value:
        return

    logger.warning("Chaos Engine active: Injecting %r into %s", mode_str, provider)

    if mode_str == ChaosMode.SLOW.value:
        # Gray failure: provider is responding but violating SLA
        delay = random.uniform(1.0, 3.0)
        await asyncio.sleep(delay)
        return

    if mode_str == ChaosMode.TIMEOUT.value:
        # Simulate a hard timeout
        delay = random.uniform(1.6, 2.0)  # Just over the 1.5s SLA
        await asyncio.sleep(delay)
        raise httpx.TimeoutException(
            f"Chaos Engine: Synthetic timeout for {provider}"
        )

    if mode_str == ChaosMode.RATE_LIMIT.value:
        # Simulate an immediate 429 response
        request = httpx.Request("POST", f"https://chaos.local/{provider}")
        response = httpx.Response(status_code=429, request=request)
        raise httpx.HTTPStatusError(
            f"Chaos Engine: Synthetic 429 Rate Limit for {provider}",
            request=request,
            response=response,
        )
