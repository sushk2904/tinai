"""
api/cache.py — L1 Exact-Match Prompt Cache (Phase 2.4)

Provides read access in the sync path and delegates writes to a Celery task.

Design (tradeoffs-info §1):
  SYNC PATH  → get_cached_response()      Redis GET — O(1), ~0.1ms
  ASYNC PATH → set_cached_response()      called as workers/tasks/cache.py Celery task

Cache key: SHA-256 of the raw prompt string (64-char hex)
TTL:       86400s (24h) per PRD §3.9
Value:     JSON-serialised ProviderResponse fields (output_text, token_count, etc.)

Cache hit rate target (PRD §3.9): >60% for repeated prompt workloads.
A cache hit reduces p50 latency from ~600ms to <5ms.
"""

import hashlib
import json
import logging
from typing import Optional

from redis.asyncio import Redis

from api.redis_keys import key_prompt_cache

logger = logging.getLogger("tinai.cache")


def hash_prompt(prompt: str) -> str:
    """
    Compute the SHA-256 hex digest of the prompt string.
    Used as the cache key suffix and as the L1 cache lookup key.

    Returns a 64-character lowercase hex string — matches the validation
    constraint in key_prompt_cache() which requires exactly 64 chars.
    """
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


async def get_cached_response(prompt_hash: str, redis: Redis) -> Optional[dict]:
    """
    Look up a prompt hash in the L1 Redis cache.

    Args:
        prompt_hash: 64-char SHA-256 hex digest from hash_prompt().
        redis:       The shared Redis client from get_redis() dependency.

    Returns:
        Parsed dict (ProviderResponse fields) if cache hit, else None.

    Called from:
        api/routers/infer.py — step 4 of the sync request loop (Phase 2.6).
    """
    rkey = key_prompt_cache(prompt_hash)

    try:
        raw = await redis.get(rkey)
        if raw is None:
            return None
        return json.loads(raw)
    except json.JSONDecodeError:
        # Corrupt cache entry — treat as miss, let it be overwritten.
        logger.warning("Corrupt cache entry for key %s — treating as miss.", rkey)
        return None
    except Exception as exc:
        # Redis unavailable — fail open (serve the request uncached).
        logger.error("Cache read error for %s: %s", rkey, exc)
        return None


async def set_cached_response(
    prompt_hash: str,
    response_dict: dict,
    redis: Redis,
    ttl: int = 86400,
) -> None:
    """
    Write a response to the L1 Redis cache.

    THIS FUNCTION IS CALLED FROM workers/tasks/cache.py (Phase 3.6)
    as a fire-and-forget Celery task — NEVER inline in the FastAPI sync loop.

    Args:
        prompt_hash:   64-char SHA-256 hex digest.
        response_dict: ProviderResponse fields serialised to a plain dict.
        redis:         Redis client (async in API context, sync in Celery context).
        ttl:           Key TTL in seconds (default 86400 = 24h per PRD §3.9).

    Note: In the Celery task, the redis argument is a synchronous redis.Redis
    client — the task calls redis.set() directly. The function signature is
    kept identical so the logic lives here and the task just wraps it.
    """
    rkey = key_prompt_cache(prompt_hash)
    try:
        payload = json.dumps(response_dict, ensure_ascii=False)
        await redis.set(rkey, payload, ex=ttl)
        logger.debug("Cache SET %s (ttl=%ds)", rkey, ttl)
    except Exception as exc:
        # Cache write failure is non-fatal — the response was already returned.
        logger.error("Cache write error for %s: %s", rkey, exc)
