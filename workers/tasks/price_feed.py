"""
workers/tasks/price_feed.py — Dynamic Price Feed Engine (Phase 4.1)

Simulates live spot-pricing changes from external providers by perturbing
a multiplier for each provider. Written to Redis and consumed by the
API inference path when computing actual usage costs.

This tasks runs periodically via Celery Beat (e.g., every 15 minutes).
"""

import logging
import os
import random

import redis as redis_sync

from api.redis_keys import PROVIDERS, key_price_multiplier
from workers.celery_app import celery_app

logger = logging.getLogger("tinai.tasks.price_feed")

_REDIS_URL = os.environ.get("REDIS_URL_MAB", "redis://redis:6379/0")
_TTL = 3600  # 1 hour (peak-hour window simulation)


@celery_app.task(name="workers.tasks.price_feed.simulate_price_update", bind=True, max_retries=2)
def simulate_price_update(self) -> None:
    """
    Randomly perturbs cost multipliers for each provider and writes them
    to the MAB Redis DB. 

    The multiplier typically ranges between 0.8 and 2.5 to simulate
    a mix of off-peak discounts and peak-surge pricing.
    """
    r = redis_sync.from_url(_REDIS_URL, decode_responses=True)
    try:
        pipeline = r.pipeline()
        changes = []

        for provider in PROVIDERS:
            # Simulate a dynamic market: mostly standard, but occasionally huge surges.
            # Using random.choices for weighted probability:
            # 60% chance: roughly normal (0.8 - 1.2)
            # 30% chance: mild surge  (1.2 - 1.8)
            # 10% chance: heavy surge (1.8 - 4.5)
            tier = random.choices(["normal", "mild", "heavy"], weights=[0.6, 0.3, 0.1])[0]
            
            if tier == "normal":
                mult = random.uniform(0.8, 1.2)
            elif tier == "mild":
                mult = random.uniform(1.2, 1.8)
            else:
                mult = random.uniform(1.8, 4.5)
            
            mult = round(mult, 2)
            
            rkey = key_price_multiplier(provider)
            pipeline.set(rkey, str(mult), ex=_TTL)
            changes.append((provider, mult))

        pipeline.execute()

        for provider, mult in changes:
            logger.info("Dynamic Pricing Update: provider=%s multiplier=%.2fx", provider, mult)

    except Exception as exc:
        logger.error("Price feed simulation failed: %s — retrying", exc)
        raise self.retry(exc=exc, countdown=5)
    finally:
        r.close()
