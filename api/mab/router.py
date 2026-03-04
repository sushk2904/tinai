"""
api/mab/router.py — MAB Provider Selection (Phase 2.5)

The routing "brain" — reads MAB weights from Redis and selects a provider.
READ ONLY from Redis. No DB queries, no HTTP calls, no writes.

Selection strategy: Softmax-weighted random sampling (Thompson Sampling variant)
  1. Read all provider weights from Redis (defaults to 1.0 if missing).
  2. Filter out providers whose circuit breaker is OPEN (Phase 2.7).
  3. Apply policy modifier to the weights (latency-first, cost-first, sla-aware).
  4. Softmax-normalise the weights into a probability distribution.
  5. Sample one provider according to that distribution.

Why softmax + sampling (not argmax):
  Pure argmax (always pick highest weight) collapses to a single provider
  immediately, starving others of signal and breaking the EMA update loop.
  Softmax sampling maintains exploration while heavily favouring good providers.
  This is the "exploitation vs. exploration" tradeoff from bandit theory.

Policy modifier multipliers (applied before softmax):
  sla-aware:    weights unchanged (balanced)
  latency-first: groq × 1.5, openrouter × 0.8, fallback × 0.5
  cost-first:    openrouter × 1.5, groq × 0.8, fallback × 1.0
  These are heuristic priors that bias the distribution while the EMA
  builds up real signal. They are replaced by real weight differences
  after ~50 requests per provider.
"""

import logging
import math
import random
from typing import Optional

from redis.asyncio import Redis

from api.mab.state import get_mab_weights
from api.redis_keys import PROVIDERS, CIRCUIT_OPEN, key_circuit_state

logger = logging.getLogger("tinai.mab.router")

# Fallback is always the last resort — never selected as primary unless
# all other providers are circuit-broken.
_FALLBACK_PROVIDER = "fallback"

# Policy-specific weight multipliers (heuristic priors — replaced by EMA signal)
# Multipliers are intentionally strong (3.0×/0.3×) to overcome EMA weight proximity.
# When both providers have similar reward scores, a weak 1.5× bias gets absorbed
# by softmax. A 10:1 ratio (3.0 vs 0.3) reliably routes while staying probabilistic.
_POLICY_MULTIPLIERS: dict[str, dict[str, float]] = {
    "latency-first":  {"groq": 3.0,  "openrouter": 0.3, "fallback": 0.1},
    "cost-first":     {"groq": 0.3,  "openrouter": 3.0, "fallback": 0.5},
    "sla-aware":      {"groq": 1.0,  "openrouter": 1.0, "fallback": 1.0},
}


def _softmax(weights: dict[str, float]) -> dict[str, float]:
    """
    Softmax-normalise a dict of raw scores into a probability distribution.
    Numerically stable: subtract max before exp() to prevent overflow.
    """
    if not weights:
        return {}
    max_w = max(weights.values())
    exps  = {k: math.exp(v - max_w) for k, v in weights.items()}
    total = sum(exps.values())
    return {k: v / total for k, v in exps.items()}


def _weighted_sample(probabilities: dict[str, float]) -> str:
    """
    Sample one provider key according to the probability distribution.
    Uses random.choices which implements Walker's alias method internally.
    """
    providers = list(probabilities.keys())
    weights   = list(probabilities.values())
    return random.choices(providers, weights=weights, k=1)[0]


async def select_provider(
    policy: str,
    redis: Redis,
    exclude_open_circuits: bool = True,
) -> str:
    """
    Select a provider for the next inference request using MAB weights.

    Args:
        policy:               "sla-aware" | "latency-first" | "cost-first"
        redis:                Shared Redis client (read-only).
        exclude_open_circuits: Skip providers with OPEN circuit breakers (Phase 2.7).

    Returns:
        str: Provider name — one of PROVIDERS constant from redis_keys.py.
             Falls back to "fallback" if all primary providers are circuit-broken.

    Called from:
        api/routers/infer.py — step 6 of the sync request loop (Phase 2.6).
    """
    # Step 1: Read MAB weights for all providers
    weights = await get_mab_weights(redis)

    # Step 2: Filter out circuit-broken providers
    if exclude_open_circuits:
        available: dict[str, float] = {}
        for provider, weight in weights.items():
            if provider == _FALLBACK_PROVIDER:
                continue  # fallback handled separately
            circuit_key   = key_circuit_state(provider)
            circuit_state = await redis.get(circuit_key)
            if circuit_state == CIRCUIT_OPEN:
                logger.info("MAB: excluding %s (circuit OPEN)", provider)
            else:
                available[provider] = weight
    else:
        available = {p: w for p, w in weights.items() if p != _FALLBACK_PROVIDER}

    # Step 3: If all primary providers are circuit-broken, use fallback
    if not available:
        logger.warning("MAB: all primary circuits OPEN — routing to fallback")
        return _FALLBACK_PROVIDER

    # Step 4: Apply policy multipliers
    multipliers = _POLICY_MULTIPLIERS.get(policy, _POLICY_MULTIPLIERS["sla-aware"])
    adjusted = {p: w * multipliers.get(p, 1.0) for p, w in available.items()}

    # Step 5: Softmax + sample
    probabilities = _softmax(adjusted)
    selected      = _weighted_sample(probabilities)

    logger.debug(
        "MAB select: policy=%s probs=%s → %s",
        policy,
        {p: f"{v:.3f}" for p, v in probabilities.items()},
        selected,
    )
    return selected
