"""
api/providers/fallback.py — Internal Fallback Provider (Phase 2.3)

tradeoffs-info §5: The "Internal Fallback Model" is mathematically treated
as an internal node by the MAB router but is physically implemented as a
designated external API endpoint — never a local model.

In this system, the fallback is a highly-reliable model on Groq (or any
stable external endpoint) configured via:
  FALLBACK_API_KEY   — its own API key (may differ from GROQ_API_KEY)
  FALLBACK_MODEL     — model ID (default: llama-3.3-70b-versatile)
  FALLBACK_BASE_URL  — base URL (default: https://api.groq.com/openai/v1)

Why a separate key and URL:
  • The fallback endpoint may be on a different account with a dedicated
    rate limit budget, ensuring it is unaffected by main-path quota usage.
  • Configuring FALLBACK_BASE_URL to point to Google AI Pro or any other
    provider converts the fallback without changing code — only .env changes.

Called by:
  • api/mab/router.py — when the selected primary provider's circuit breaker
    is OPEN (Phase 2.7).
  • api/routers/infer.py — as a last resort after all providers fail.
"""

import logging
import time

import httpx

from api.config import get_settings
from api.providers.base import (
    FALLBACK_INPUT_CENTS_PER_TOKEN,
    FALLBACK_OUTPUT_CENTS_PER_TOKEN,
    ProviderResponse,
    calculate_cost_cents,
)
from api.providers.retry import provider_retry

logger = logging.getLogger("tinai.providers.fallback")
settings = get_settings()

_MAX_TOKENS = 1024


@provider_retry
async def _fallback_http_call(
    client: httpx.AsyncClient,
    model: str,
    payload: dict,
    chat_url: str,
) -> dict:
    """
    Raw HTTP POST to the fallback endpoint.
    Retried once on ConnectError/RemoteProtocolError.
    """
    response = await client.post(chat_url, json=payload)
    response.raise_for_status()
    return response.json()


async def call_fallback(
    prompt: str,
    model: str = "",
    timeout: float = 1.5,
    price_multiplier: float = 1.0,
    redis=None,
) -> ProviderResponse:
    """
    Call the designated fallback external API endpoint.

    This is the provider of last resort — called when all primary providers
    have tripped their circuit breakers or returned errors. It MUST be the
    most reliable provider in the pool.

    Args:
        prompt:           Raw user prompt.
        model:            Model ID. Defaults to settings.fallback_model.
        timeout:          TTFB timeout (PRD §3.9 SLA = 1.5s).
        price_multiplier: Dynamic pricing factor (Phase 4.1).

    Returns:
        ProviderResponse — always. Never raises.
    """
    start       = time.perf_counter()
    model       = (model or settings.fallback_model)  # guard empty string
    chat_url    = f"{settings.fallback_base_url.rstrip('/')}/chat/completions"

    payload = {
        "model":      model,
        "messages":   [{"role": "user", "content": prompt}],
        "max_tokens": _MAX_TOKENS,
    }

    effective_timeout = httpx.Timeout(connect=timeout, read=timeout, write=5.0, pool=1.0)

    try:
        async with httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {settings.fallback_api_key}",
                "Content-Type":  "application/json",
            },
            timeout=effective_timeout,
        ) as client:
            if redis:
                from api.chaos import inject_chaos
                await inject_chaos("fallback", redis)
            data = await _fallback_http_call(client, model, payload, chat_url)

        latency_ms = int((time.perf_counter() - start) * 1000)

        output_text       = data["choices"][0]["message"]["content"]
        usage             = data.get("usage", {})
        prompt_tokens     = usage.get("prompt_tokens",     0)
        completion_tokens = usage.get("completion_tokens", 0)
        total_tokens      = usage.get("total_tokens",      prompt_tokens + completion_tokens)
        actual_model      = data.get("model", model)

        cost_cents = calculate_cost_cents(
            prompt_tokens,
            completion_tokens,
            FALLBACK_INPUT_CENTS_PER_TOKEN  * price_multiplier,
            FALLBACK_OUTPUT_CENTS_PER_TOKEN * price_multiplier,
        )

        logger.debug(
            "Fallback OK — model=%s latency=%dms tokens=%d cost=%.4f¢",
            actual_model, latency_ms, total_tokens, cost_cents,
        )

        return ProviderResponse(
            latency_ms=latency_ms,
            token_count=total_tokens,
            cost_cents=cost_cents,
            error_flag=False,
            output_text=output_text,
            provider="fallback",
            model=actual_model,
        )

    except httpx.TimeoutException as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        logger.error(
            "Fallback TIMEOUT after %dms — all providers exhausted: %s",
            latency_ms, exc,
        )
        return ProviderResponse(latency_ms=latency_ms, error_flag=True, provider="fallback", model=model)

    except httpx.HTTPStatusError as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        logger.error("Fallback HTTP %d after %dms: %s", exc.response.status_code, latency_ms, exc)
        return ProviderResponse(latency_ms=latency_ms, error_flag=True, provider="fallback", model=model)

    except Exception as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        logger.error("Fallback FAILED after %dms (%s): %s", latency_ms, type(exc).__name__, exc)
        return ProviderResponse(latency_ms=latency_ms, error_flag=True, provider="fallback", model=model)
