"""
api/mab/state.py — MAB Redis State Readers (Phase 2.5)

READ-ONLY from Redis. No writes, no side effects.
tradeoffs-info §1: The sync request loop may ONLY read from Redis.
All writes happen in workers/tasks/telemetry.py (Celery async).

Two functions:
  get_mab_weights()  → {provider: weight}    used by select_provider()
  get_ema_stats()    → (mu, var)              used by update_mab_weights task

Cold-start defaults:
  - mab:weights:{provider} missing → default weight 1.0 (uniform distribution)
  - mab:stats:{provider}:{metric}:mu missing → 0.0
  - mab:stats:{provider}:{metric}:var missing → 1.0 (broad prior, avoids cold-start Z-score explosion)
"""

import logging
from typing import Optional

from redis.asyncio import Redis

from api.redis_keys import PROVIDERS, METRICS, key_mab_weights, key_mab_stats_mu, key_mab_stats_var

logger = logging.getLogger("tinai.mab.state")

# Default weight when a provider has no history yet — uniform prior
_DEFAULT_WEIGHT: float = 1.0
# Default variance prior — broad uncertainty on cold start
_DEFAULT_MU:  float = 0.0
_DEFAULT_VAR: float = 1.0


async def get_mab_weights(redis: Redis) -> dict[str, float]:
    """
    Read MAB reward weights for all registered providers from Redis.

    Returns:
        dict mapping provider name → weight float.
        Missing keys default to 1.0 (uniform — no preference yet).

    Called from:
        api/mab/router.py select_provider() — step 6 of the sync request loop.
    """
    weights: dict[str, float] = {}

    for provider in PROVIDERS:
        rkey = key_mab_weights(provider)
        raw = await redis.get(rkey)
        if raw is None:
            weights[provider] = _DEFAULT_WEIGHT
            logger.debug("MAB: no weight for %s — using default %.2f", provider, _DEFAULT_WEIGHT)
        else:
            try:
                weights[provider] = float(raw)
            except (ValueError, TypeError):
                logger.warning("MAB: corrupt weight for %s (%r) — using default", provider, raw)
                weights[provider] = _DEFAULT_WEIGHT

    return weights


async def get_ema_stats(
    provider: str,
    metric: str,
    redis: Redis,
) -> tuple[float, float]:
    """
    Read EMA mean (μ) and variance (σ²) for a provider+metric pair.

    Returns:
        tuple (mu, var) — both floats, defaulting to (0.0, 1.0) on cold start.

    Called from:
        workers/tasks/telemetry.py update_mab_weights — after each inference.
    """
    mu_raw  = await redis.get(key_mab_stats_mu(provider, metric))
    var_raw = await redis.get(key_mab_stats_var(provider, metric))

    try:
        mu = float(mu_raw) if mu_raw is not None else _DEFAULT_MU
    except (ValueError, TypeError):
        mu = _DEFAULT_MU

    try:
        var = float(var_raw) if var_raw is not None else _DEFAULT_VAR
    except (ValueError, TypeError):
        var = _DEFAULT_VAR

    return mu, var
