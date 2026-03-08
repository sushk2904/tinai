"""
workers/tasks/telemetry.py — Inference Telemetry & MAB Update Tasks (Phase 3.2)

THE ONLY place PostgreSQL writes happen for inference data.
ALL writes from every FastAPI worker go through this Celery task.
tradeoffs-info §1: No synchronous DB write in the request loop.

Two tasks:
  log_inference_telemetry — INSERT into inference_logs
  update_mab_weights      — EMA update + Redis write + Postgres backup
"""

import asyncio
import logging
import os

import asyncpg
import redis as redis_sync

from api.mab.reward import compute_z_score, compute_reward
from api.redis_keys import (
    METRICS, key_mab_weights, key_mab_stats_mu, key_mab_stats_var,
)
from workers.celery_app import celery_app

logger = logging.getLogger("tinai.tasks.telemetry")

_DATABASE_URL = os.environ.get("DATABASE_URL", "").replace("postgresql+asyncpg://", "postgresql://")
_REDIS_URL    = os.environ.get("REDIS_URL_MAB", "redis://redis:6379/0")

# EMA decay factor λ (tradeoffs-info §2.1)
_LAMBDA = float(os.environ.get("MAB_EMA_LAMBDA", "0.1"))

# MAB reward weights
_ALPHA = float(os.environ.get("MAB_ALPHA", "1.0"))
_BETA  = float(os.environ.get("MAB_BETA",  "0.5"))
_GAMMA = float(os.environ.get("MAB_GAMMA", "0.5"))


@celery_app.task(name="workers.tasks.telemetry.log_inference_telemetry", bind=True, max_retries=3)
def log_inference_telemetry(self, payload: dict) -> None:
    """
    INSERT one row into inference_logs from the inference event payload.

    Expected payload keys:
        request_id, provider, model, latency_ms, token_count,
        cost_cents, error_flag, prompt_hash, client_key

    Retried up to 3 times on DB connection errors (exponential backoff).
    Uses a fresh asyncpg connection per task (no shared pool in Celery workers).
    """
    async def _insert():
        conn = await asyncpg.connect(_DATABASE_URL)
        try:
            await conn.execute(
                """
                INSERT INTO inference_logs
                    (request_id, provider, model, policy, latency_ms,
                     token_count, cost_cents, error_flag, prompt_hash, client_key, quality_score)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                payload.get("request_id"),
                payload.get("provider"),
                payload.get("model"),
                payload.get("policy", "unknown"),
                payload.get("latency_ms", 0),
                payload.get("token_count"),
                payload.get("cost_cents", 0.0),
                payload.get("error_flag", False),
                payload.get("prompt_hash"),
                payload.get("client_key"),
                payload.get("quality_score", 1.0),
            )
        finally:
            await conn.close()

    try:
        asyncio.run(_insert())
        logger.debug("Telemetry logged for request %s", payload.get("request_id"))
    except Exception as exc:
        logger.error("Telemetry insert failed: %s — retrying", exc)
        raise self.retry(exc=exc, countdown=2 ** self.request.retries)


@celery_app.task(name="workers.tasks.telemetry.update_mab_weights", bind=True, max_retries=3)
def update_mab_weights(
    self,
    provider: str,
    latency_ms: int,
    cost_cents: float,
    quality_score: float,
) -> None:
    """
    EMA update for a single provider after an inference response.

    Steps:
      1. Read current (mu, var) for latency, cost, quality from Redis.
      2. Apply EMA: μ_t = (1-λ)μ_{t-1} + λX_t
                    σ²_t = (1-λ)σ²_{t-1} + λ(X_t - μ_t)²
      3. Compute Z-scores and composite reward R.
      4. Write updated stats + new weight back to Redis.
      5. Persist to provider_stats Postgres table (durable backup).

    Args:
        provider:      Provider name ("groq", "openrouter", "fallback").
        latency_ms:    Wall-clock latency of the completed request.
        cost_cents:    Cost in USD cents.
        quality_score: Safety/quality score (0.0–1.0). Defaults to 1.0 if no
                       hallucination check was run (sampled at SAFETY_SAMPLE_RATE).
    """
    r = redis_sync.from_url(_REDIS_URL, decode_responses=True)

    try:
        observations = {
            "latency": float(latency_ms),
            "cost":    cost_cents,
            "quality": quality_score,
        }

        new_mu:  dict[str, float] = {}
        new_var: dict[str, float] = {}
        z_scores: dict[str, float] = {}

        for metric in METRICS:
            x = observations[metric]

            mu_raw  = r.get(key_mab_stats_mu(provider, metric))
            var_raw = r.get(key_mab_stats_var(provider, metric))
            mu  = float(mu_raw)  if mu_raw  else 0.0
            var = float(var_raw) if var_raw else 1.0

            # EMA update (tradeoffs-info §2.1)
            mu_new  = (1 - _LAMBDA) * mu + _LAMBDA * x
            var_new = (1 - _LAMBDA) * var + _LAMBDA * (x - mu_new) ** 2

            new_mu[metric]  = mu_new
            new_var[metric] = var_new
            z_scores[metric] = compute_z_score(x, mu_new, var_new)

            # Write updated EMA stats to Redis
            r.set(key_mab_stats_mu(provider, metric),  str(mu_new))
            r.set(key_mab_stats_var(provider, metric), str(var_new))

        # Compute composite reward and update MAB weight
        reward = compute_reward(
            z_quality=z_scores["quality"],
            z_latency=z_scores["latency"],
            z_cost=z_scores["cost"],
            alpha=_ALPHA,
            beta=_BETA,
            gamma=_GAMMA,
        )
        r.set(key_mab_weights(provider), str(reward))
        logger.debug("MAB updated: %s reward=%.4f", provider, reward)

        # Persist to Postgres (durable backup for dashboard / drift analysis)
        async def _persist():
            conn = await asyncpg.connect(_DATABASE_URL)
            try:
                await conn.execute(
                    """
                    INSERT INTO provider_stats
                        (provider, ema_latency_mu, ema_latency_var,
                         ema_cost_mu, ema_cost_var, ema_quality_mu, ema_quality_var)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (provider) DO UPDATE SET
                        ema_latency_mu  = EXCLUDED.ema_latency_mu,
                        ema_latency_var = EXCLUDED.ema_latency_var,
                        ema_cost_mu     = EXCLUDED.ema_cost_mu,
                        ema_cost_var    = EXCLUDED.ema_cost_var,
                        ema_quality_mu  = EXCLUDED.ema_quality_mu,
                        ema_quality_var = EXCLUDED.ema_quality_var,
                        updated_at      = NOW()
                    """,
                    provider,
                    new_mu["latency"],  new_var["latency"],
                    new_mu["cost"],     new_var["cost"],
                    new_mu["quality"],  new_var["quality"],
                )
            finally:
                await conn.close()

        asyncio.run(_persist())

    except Exception as exc:
        logger.error("MAB update failed for %s: %s — retrying", provider, exc)
        raise self.retry(exc=exc, countdown=2 ** self.request.retries)
    finally:
        r.close()
