"""
api/routers/infer.py — POST /v1/infer Endpoint (Phase 2.6)

The main Data Plane route. Executes the 9-step sync request loop
defined in TODO §2.6, firing Celery tasks for everything non-blocking.

Sync path (must stay under 1500ms total):
  1. verify_api_key()          → client_key
  2. check_rate_limit()        → 429 if exhausted
  3. check_budget()            → 402 if exceeded
  4. hash_prompt → L1 cache GET  → cache hit returns in <5ms
  5. select_provider() (MAB)   → provider name string
  6. call provider async       → ProviderResponse (≤1500ms hard timeout)
  7. Return response to client

Post-response (non-blocking, fire-and-forget Celery):
  8. log_inference_telemetry.delay()
  9. update_mab_weights.delay()
  10. populate_cache.delay()
  11. deduct_budget.delay()
  12. run_hallucination_check.delay()  [if sampled]
"""

import json
import logging
import random

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from api.auth import ClientKeyDep
from api.budget_guard import check_budget
from api.cache import get_cached_response, hash_prompt
from api.circuit_breaker import record_failure, record_success
from api.config import get_settings
from api.dependencies import RedisDep
from api.mab.router import select_provider
from api.providers import PROVIDER_MAP

logger   = logging.getLogger("tinai.routers.infer")
router   = APIRouter()
settings = get_settings()


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class InferRequest(BaseModel):
    prompt:   str  = Field(..., min_length=1, max_length=32_000, description="The user prompt.")
    model:    str  = Field("", description="Optional provider-specific model override.")
    policy:   str  = Field("sla-aware", description="Routing policy: sla-aware | latency-first | cost-first")
    provider: str  = Field("", description="[DEV ONLY] Force a specific provider, bypassing MAB. e.g. 'groq'")


class InferResponse(BaseModel):
    output_text:  str
    provider:     str
    model:        str
    latency_ms:   int
    token_count:  int | None
    cost_cents:   float
    cache_hit:    bool
    request_id:   str


# ---------------------------------------------------------------------------
# POST /v1/infer
# ---------------------------------------------------------------------------

@router.post(
    "/infer",
    response_model=InferResponse,
    summary="Route a prompt to the optimal LLM provider via MAB.",
    description=(
        "Adaptive inference endpoint. Selects the optimal provider using "
        "Multi-Armed Bandit weights, enforces rate limiting and budget guards, "
        "and returns the LLM response within the 1500ms SLA."
    ),
)
async def infer(
    body:       InferRequest,
    request:    Request,
    client_key: ClientKeyDep,
    redis:      RedisDep,
) -> InferResponse:
    request_id = request.state.request_id

    # --- Step 1.5: Load Shedding --------------------------------------------
    from api.load_shedder import should_shed
    if await should_shed(redis):
        from fastapi import HTTPException
        logger.warning("Request %s Rejected — Load Shedding ACTIVE", request_id)
        raise HTTPException(status_code=503, detail="Service Unavailable: System Overloaded")

    # --- Step 2: Rate limit check -------------------------------------------
    from api.rate_limiter import check_rate_limit
    await check_rate_limit(client_key, redis)

    # --- Step 3: Budget guard ------------------------------------------------
    await check_budget(client_key, redis)

    # --- Step 4: L1 cache lookup ---------------------------------------------
    prompt_hash = hash_prompt(body.prompt)
    cached      = await get_cached_response(prompt_hash, redis)

    if cached:
        logger.debug("Cache HIT for request %s", request_id)
        # Fire telemetry for cache hit (cost=0, latency≈0)
        _fire_log_task(
            request_id=request_id,
            provider=cached.get("provider", "cache"),
            model=cached.get("model", ""),
            policy=body.policy or "latency-first",
            latency_ms=0,
            token_count=cached.get("token_count"),
            cost_cents=0.0,
            error_flag=False,
            prompt_hash=prompt_hash,
            client_key=client_key,
        )
        return InferResponse(
            output_text=cached["output_text"],
            provider=cached.get("provider", "cache"),
            model=cached.get("model", ""),
            latency_ms=0,
            token_count=cached.get("token_count"),
            cost_cents=0.0,
            cache_hit=True,
            request_id=request_id,
        )

    # --- Step 5+6: MAB provider selection + LLM call ------------------------
    if body.provider and settings.environment == "dev" and body.provider in PROVIDER_MAP:
        provider_name = body.provider
        logger.info("Request %s → FORCED provider=%s (dev override)", request_id, provider_name)
    else:
        provider_name = await select_provider(body.policy, redis)
        logger.info("Request %s → MAB selected provider=%s policy=%s", request_id, provider_name, body.policy)

    call_fn = PROVIDER_MAP[provider_name]

    # --- Step 5.5: Read dynamic price multiplier (Phase 4.1) ----------------
    from api.redis_keys import key_price_multiplier
    pm_str = await redis.get(key_price_multiplier(provider_name))
    price_multiplier = float(pm_str) if pm_str else 1.0

    provider_response = await call_fn(
        prompt=body.prompt,
        model=body.model or "",
        timeout=settings.llm_timeout_seconds,
        price_multiplier=price_multiplier,
        redis=redis,
    )

    # --- Steps 8–12: Fire-and-forget Celery tasks (Must trigger BEFORE raising 503) ---
    _fire_post_response_tasks(
        request_id=request_id,
        provider_name=provider_name,
        provider_response=provider_response,
        prompt=body.prompt,
        prompt_hash=prompt_hash,
        client_key=client_key,
        quality_score=1.0,  # Updated by safety task if sampled
        policy=body.policy or "latency-first",
    )

    # --- Step 7: Handle Error Responses --------------------------------------
    if provider_response.error_flag:
        _fire_circuit_failure(provider_name, redis)
        from fastapi import HTTPException
        raise HTTPException(
            status_code=503,
            detail=f"Provider '{provider_name}' failed. Please retry.",
            headers={"X-Provider": provider_name, "X-Request-ID": request_id},
        )

    # Record success into circuit breaker (closes/keeps circuit CLOSED)
    try:
        import asyncio
        from api.circuit_breaker import record_success
        asyncio.ensure_future(record_success(provider_name, redis))
    except Exception as e:
        logger.debug("Circuit success record error (non-critical): %s", e)

    return InferResponse(
        output_text=provider_response.output_text or "",
        provider=provider_response.provider,
        model=provider_response.model,
        latency_ms=provider_response.latency_ms,
        token_count=provider_response.token_count,
        cost_cents=provider_response.cost_cents,
        cache_hit=False,
        request_id=request_id,
    )


# ---------------------------------------------------------------------------
# Fire-and-forget helpers — keep the route body readable
# ---------------------------------------------------------------------------

def _fire_log_task(**kwargs) -> None:
    try:
        from workers.tasks.telemetry import log_inference_telemetry
        log_inference_telemetry.delay(kwargs)
    except Exception as e:
        logger.error("Failed to enqueue telemetry task: %s", e)


def _fire_circuit_failure(provider: str, redis) -> None:
    """Record a provider failure into the circuit breaker (async Redis write)."""
    try:
        import asyncio
        from api.circuit_breaker import record_failure
        # We're in a sync context — run the coroutine on the running event loop
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(record_failure(provider, redis))
        else:
            loop.run_until_complete(record_failure(provider, redis))
        logger.warning("Circuit failure recorded for %s", provider)
    except Exception as e:
        logger.error("Circuit failure recording error: %s", e)


def _fire_post_response_tasks(
    request_id:        str,
    provider_name:     str,
    provider_response,
    prompt:            str,
    prompt_hash:       str,
    client_key:        str,
    quality_score:     float,
    policy:            str = "latency-first",
) -> None:
    """Enqueue all fire-and-forget Celery tasks post-response."""
    try:
        from workers.tasks.telemetry import log_inference_telemetry, update_mab_weights
        from workers.tasks.cache     import populate_cache
        from workers.tasks.budget    import deduct_budget
        from workers.tasks.observability import send_langfuse_trace

        payload = {
            "request_id":  request_id,
            "provider":    provider_response.provider,
            "model":       provider_response.model,
            "policy":      policy,
            "latency_ms":  provider_response.latency_ms,
            "token_count": provider_response.token_count,
            "cost_cents":  provider_response.cost_cents,
            "error_flag":  provider_response.error_flag,
            "prompt_hash": prompt_hash,
            "client_key":  client_key,
        }
        log_inference_telemetry.delay(payload)

        # ── Quality-aware MAB routing ────────────────────────────────────────
        # QUALITY_SAMPLE_RATE (30%) of requests get LLM-judged quality scores
        # via run_quality_eval → update_mab_weights (chained).
        # The remaining 70% get an immediate MAB update with quality=1.0 so
        # the EMA always has fresh latency + cost signal even without quality.
        if (provider_response.output_text
                and random.random() < settings.quality_sample_rate):
            try:
                from workers.tasks.quality import run_quality_eval
                run_quality_eval.delay(
                    prompt,
                    provider_response.output_text,
                    request_id,
                    provider_response.provider,
                    provider_response.model,
                    provider_response.latency_ms,
                    provider_response.cost_cents,
                )
                logger.debug(
                    "Quality eval enqueued for %s (provider=%s)",
                    request_id, provider_response.provider,
                )
            except Exception as e:
                # Fall back to immediate update with default quality
                logger.warning("Quality task enqueue failed (%s) — using default 1.0", e)
                update_mab_weights.delay(
                    provider_name,
                    provider_response.latency_ms,
                    provider_response.cost_cents,
                    1.0,
                )
        else:
            # Unsampled: immediate MAB update with quality=1.0 assumption
            update_mab_weights.delay(
                provider_name,
                provider_response.latency_ms,
                provider_response.cost_cents,
                1.0,
            )

        # ── Cache population ─────────────────────────────────────────────────
        if provider_response.output_text:
            response_json = json.dumps({
                "output_text": provider_response.output_text,
                "provider":    provider_response.provider,
                "model":       provider_response.model,
                "token_count": provider_response.token_count,
            })
            populate_cache.delay(prompt_hash, response_json)

        # ── Budget deduction ─────────────────────────────────────────────────
        if provider_response.cost_cents > 0:
            deduct_budget.delay(client_key, provider_response.cost_cents)

        # ── Safety sampling (binary SAFE/UNSAFE — separate from quality) ─────
        if (provider_response.output_text
                and random.random() < settings.safety_sample_rate):
            from workers.tasks.safety import run_hallucination_check
            run_hallucination_check.delay(
                prompt,
                provider_response.output_text,
                request_id,
            )

        # ── Observability Tracing ────────────────────────────────────────────
        if provider_response.output_text and not provider_response.error_flag:
            send_langfuse_trace.delay(
                request_id,
                prompt,
                provider_response.output_text,
                provider_response.provider,
                provider_response.latency_ms,
                provider_response.cost_cents,
                provider_response.model or "",
            )

    except Exception as e:
        # Post-response tasks failing must NEVER affect the returned response.
        logger.error("Post-response task enqueue error: %s", e)

