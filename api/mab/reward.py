"""
api/mab/reward.py — MAB Reward Computation (Phase 2.5)

Pure computation — NO Redis reads, NO network I/O, NO side effects.
Every function here is a deterministic mathematical transform.

Formulas from tradeoffs-info §2:
  Z-score:  Z = (x - μ) / max(σ, ε)
  Reward:   R = α·Z_quality - β·Z_latency - γ·Z_cost

The Z-score normalisation ensures that three metrics with completely
different units and magnitudes (ms, cents, 0-1 score) are comparable
on the same scale before the weighted reward is computed.

Why ε (epsilon) in the denominator:
  On the very first few requests, σ² (EMA variance) is near zero because
  we initialised it to 1.0 in ProviderStats but haven't seen enough samples
  to build a real distribution. Without ε, Z = x/0 = ±inf, which would
  dominate the reward and cause the MAB to make wildly wrong routing decisions
  for the first ~10 requests per provider (cold-start instability).
  ε=1e-5 bounds the Z-score to ±x/1e-5 which is large but finite.
"""

import math


def compute_z_score(
    x: float,
    mu: float,
    var: float,
    epsilon: float = 1e-5,
) -> float:
    """
    Compute the EMA Z-score for a single observation.

    Formula (tradeoffs-info §2.2):
        Z = (x - μ) / max(√σ², ε)

    Args:
        x:       Current observation value.
        mu:      EMA running mean (μ_t from Redis).
        var:     EMA running variance (σ²_t from Redis).
        epsilon: Minimum denominator to prevent division-by-zero on cold start.

    Returns:
        float: Normalised Z-score. Positive means above average, negative below.

    Examples:
        compute_z_score(600, 500, 10000) → (600-500)/100 = 1.0  (1σ above mean latency)
        compute_z_score(0.001, 0.002, 1e-6) → (0.001-0.002)/0.001 = -1.0  (below mean cost)
    """
    sigma = math.sqrt(max(var, 0.0))   # guard against negative variance (float rounding)
    denom = max(sigma, epsilon)
    return (x - mu) / denom


def compute_reward(
    z_quality: float,
    z_latency: float,
    z_cost: float,
    alpha: float,
    beta: float,
    gamma: float,
) -> float:
    """
    Compute the composite MAB reward score for a provider.

    Formula (tradeoffs-info §2.3):
        R = α·Z_quality - β·Z_latency - γ·Z_cost

    Note the signs:
      + Z_quality: higher quality is better (reward increases)
      - Z_latency: higher latency is worse (penalty)
      - Z_cost:    higher cost is worse    (penalty)

    The weights α, β, γ are loaded from settings (api/config.py):
      • latency-first policy:  β=1.5, α=1.0, γ=0.3
      • cost-first policy:     γ=1.5, α=1.0, β=0.3
      • sla-aware policy:      α=1.0, β=0.5, γ=0.5  (balanced — default)

    Args:
        z_quality: Normalised quality Z-score (positive = good output).
        z_latency: Normalised latency Z-score (positive = slow).
        z_cost:    Normalised cost Z-score    (positive = expensive).
        alpha:     Quality weight from settings.mab_alpha.
        beta:      Latency penalty weight from settings.mab_beta.
        gamma:     Cost penalty weight from settings.mab_gamma.

    Returns:
        float: Composite reward. Higher is better. Used to rank providers
               in select_provider() for the next routing decision.
    """
    return (alpha * z_quality) - (beta * z_latency) - (gamma * z_cost)
