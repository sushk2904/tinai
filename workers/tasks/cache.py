"""
workers/tasks/cache.py — Cache Population Task (Phase 3.6)

Fire-and-forget: writes the prompt response to Redis L1 cache after
the response has already been returned to the client.
Cache misses on the next request are acceptable — this is best-effort.
"""

import logging
import os

import redis as redis_sync

from workers.celery_app import celery_app

logger = logging.getLogger("tinai.tasks.cache")

_REDIS_URL = os.environ.get("REDIS_URL_MAB", "redis://redis:6379/0")


@celery_app.task(name="workers.tasks.cache.populate_cache", bind=True, max_retries=2)
def populate_cache(self, prompt_hash: str, response_json: str, ttl: int = 86400) -> None:
    """
    Write the JSON-serialised response to Redis L1 cache.

    Args:
        prompt_hash:   64-char SHA-256 hex digest of the original prompt.
        response_json: JSON string of ProviderResponse fields.
        ttl:           Key TTL in seconds (default 86400 = 24h per PRD §3.9).
    """
    from api.redis_keys import key_prompt_cache

    r = redis_sync.from_url(_REDIS_URL, decode_responses=True)
    try:
        rkey = key_prompt_cache(prompt_hash)
        r.set(rkey, response_json, ex=ttl)
        logger.debug("Cache populated: %s (ttl=%ds)", rkey, ttl)
    except Exception as exc:
        logger.error("Cache write failed for %s: %s — retrying", prompt_hash, exc)
        raise self.retry(exc=exc, countdown=1)
    finally:
        r.close()
