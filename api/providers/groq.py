"""
api/providers/groq.py — Groq LLM Provider Client (Phase 2.3)

Implements the Provider Abstraction Layer for Groq's OpenAI-compatible API.

Key invariants (tradeoffs-info + PRD):
  • Hard TTFB timeout of 1500ms — enforced via httpx.Timeout(connect=1.5, read=1.5)
  • NEVER raises — all exceptions are caught; returns ProviderResponse(error_flag=True)
  • Cost in USD cents — calculated from usage block, never from estimates
  • Wall-clock latency starts BEFORE first attempt, ends AFTER last attempt
    (so retry time is included in the latency figure stored in InferenceLog)

Retry:
  The inner `_groq_http_call` function is decorated with `@provider_retry`.
  It retries ONCE on ConnectError/RemoteProtocolError (transient transport
  failures). TimeoutException and HTTPStatusError are not retried.

Phase 4.1 hook:
  `call_groq()` accepts an optional `price_multiplier` float (default 1.0).
  The Phase 4.1 simulate_price_update task writes this to Redis; the infer
  route reads it and passes it here. Until Phase 4.1 is implemented, it
  defaults to 1.0 (no surge pricing).
"""

import logging
import time
from typing import Optional

import httpx

from api.config import get_settings
from api.providers.base import (
    GROQ_INPUT_CENTS_PER_TOKEN,
    GROQ_OUTPUT_CENTS_PER_TOKEN,
    ProviderResponse,
    calculate_cost_cents,
)
from api.providers.retry import provider_retry

logger = logging.getLogger("tinai.providers.groq")
settings = get_settings()

# Groq OpenAI-compatible endpoint
_GROQ_BASE_URL    = "https://api.groq.com/openai/v1"
_GROQ_CHAT_URL    = f"{_GROQ_BASE_URL}/chat/completions"
_DEFAULT_MODEL    = "llama-3.3-70b-versatile"
_MAX_TOKENS       = 1024

# Hard TTFB timeout per PRD §3.9 — do NOT change without updating the SLA doc.
_TIMEOUT = httpx.Timeout(connect=1.5, read=1.5, write=5.0, pool=1.0)


# ---------------------------------------------------------------------------
# Inner HTTP call — decorated with shared retry (ConnectError / RemoteProtocol)
# ---------------------------------------------------------------------------

@provider_retry
async def _groq_http_call(
    client: httpx.AsyncClient,
    model: str,
    payload: dict,
) -> dict:
    """
    Raw HTTP POST to Groq. Decorated with @provider_retry so this function
    is retried (at most once) on transient transport failures.

    Raises:
        httpx.ConnectError        → retried by provider_retry
        httpx.RemoteProtocolError → retried by provider_retry
        httpx.TimeoutException    → NOT retried, propagates to outer function
        httpx.HTTPStatusError     → NOT retried (4xx/5xx), propagates upward
    """
    response = await client.post(_GROQ_CHAT_URL, json=payload)
    # Raise immediately on any non-2xx — 4xx/5xx are not retried.
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# Public interface — safe, never raises
# ---------------------------------------------------------------------------

async def call_groq(
    prompt: str,
    model: str = _DEFAULT_MODEL,
    timeout: float = 1.5,
    price_multiplier: float = 1.0,
    redis=None,
) -> ProviderResponse:
    """
    Call the Groq LLM API and return a normalised ProviderResponse.

    Args:
        prompt:           The raw user prompt string.
        model:            Groq model ID (default: llama-3.3-70b-versatile).
        timeout:          TTFB timeout in seconds (default 1.5 = PRD §3.9 SLA).
        price_multiplier: Dynamic pricing factor from Redis (Phase 4.1).
                          Default 1.0 (no surge). Values > 1.0 simulate peak-
                          hour price surges that force the MAB to re-route.

    Returns:
        ProviderResponse — always. Never raises.
    """
    # Wall-clock start: before first attempt (includes retry wait if triggered).
    start = time.perf_counter()

    model   = model or _DEFAULT_MODEL  # guard against empty-string from infer route
    payload = {
        "model":      model,
        "messages":   [{"role": "user", "content": prompt}],
        "max_tokens": _MAX_TOKENS,
    }

    effective_timeout = httpx.Timeout(connect=timeout, read=timeout, write=5.0, pool=1.0)

    try:
        async with httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {settings.groq_api_key}",
                "Content-Type":  "application/json",
            },
            timeout=effective_timeout,
        ) as client:
            if redis:
                from api.chaos import inject_chaos
                await inject_chaos("groq", redis)
            data = await _groq_http_call(client, model, payload)

        latency_ms = int((time.perf_counter() - start) * 1000)

        # --- Parse response -------------------------------------------------
        output_text = data["choices"][0]["message"]["content"]
        usage       = data.get("usage", {})
        prompt_tokens     = usage.get("prompt_tokens",     0)
        completion_tokens = usage.get("completion_tokens", 0)
        total_tokens      = usage.get("total_tokens",      prompt_tokens + completion_tokens)
        actual_model      = data.get("model", model)

        cost_cents = calculate_cost_cents(
            prompt_tokens,
            completion_tokens,
            GROQ_INPUT_CENTS_PER_TOKEN  * price_multiplier,
            GROQ_OUTPUT_CENTS_PER_TOKEN * price_multiplier,
        )

        logger.debug(
            "Groq OK — model=%s latency=%dms tokens=%d cost=%.4f¢",
            actual_model, latency_ms, total_tokens, cost_cents,
        )

        return ProviderResponse(
            latency_ms=latency_ms,
            token_count=total_tokens,
            cost_cents=cost_cents,
            error_flag=False,
            output_text=output_text,
            provider="groq",
            model=actual_model,
        )

    except httpx.TimeoutException as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        logger.warning("Groq TIMEOUT after %dms: %s", latency_ms, exc)
        return ProviderResponse(
            latency_ms=latency_ms,
            error_flag=True,
            provider="groq",
            model=model,
        )

    except httpx.HTTPStatusError as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        logger.warning(
            "Groq HTTP error %d after %dms: %s",
            exc.response.status_code, latency_ms, exc,
        )
        return ProviderResponse(
            latency_ms=latency_ms,
            error_flag=True,
            provider="groq",
            model=model,
        )

    except Exception as exc:
        # Catches ConnectError/RemoteProtocolError after retries exhausted,
        # plus any unexpected parsing errors.
        latency_ms = int((time.perf_counter() - start) * 1000)
        logger.error(
            "Groq FAILED after %dms (%s): %s",
            latency_ms, type(exc).__name__, exc,
        )
        return ProviderResponse(
            latency_ms=latency_ms,
            error_flag=True,
            provider="groq",
            model=model,
        )
