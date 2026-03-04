"""
api/providers/openrouter.py — OpenRouter LLM Provider Client (Phase 2.3)

OpenRouter exposes an OpenAI-compatible API so the implementation is
structurally identical to groq.py. Key differences:
  • Base URL: https://openrouter.ai/api/v1
  • Auth header: "Authorization: Bearer {OPENROUTER_API_KEY}"
  • Requires extra headers: "HTTP-Referer" and "X-Title" (OpenRouter policy)
  • Pricing: lower than Groq for most models (OR_INPUT/OUTPUT_CENTS_PER_TOKEN)
  • Default model: meta-llama/llama-3.3-70b-instruct (free tier available)

All invariants identical to groq.py:
  • Hard 1500ms TTFB timeout
  • NEVER raises — always returns ProviderResponse
  • Cost in USD cents
  • Wall-clock latency covers all retry attempts
"""

import logging
import time

import httpx

from api.config import get_settings
from api.providers.base import (
    OR_INPUT_CENTS_PER_TOKEN,
    OR_OUTPUT_CENTS_PER_TOKEN,
    ProviderResponse,
    calculate_cost_cents,
)
from api.providers.retry import provider_retry

logger = logging.getLogger("tinai.providers.openrouter")
settings = get_settings()

_OR_CHAT_URL   = "https://openrouter.ai/api/v1/chat/completions"
_DEFAULT_MODEL = "meta-llama/llama-3.3-70b-instruct"
_MAX_TOKENS    = 1024

# OpenRouter policy: requests without these headers may be deprioritised.
_OR_EXTRA_HEADERS = {
    "HTTP-Referer": "https://github.com/sushk2904/tinai",
    "X-Title":      "TINAI Adaptive Routing Engine",
}


@provider_retry
async def _openrouter_http_call(
    client: httpx.AsyncClient,
    model: str,
    payload: dict,
) -> dict:
    """
    Raw HTTP POST to OpenRouter. Retried once on ConnectError/RemoteProtocol.
    TimeoutException and HTTPStatusError propagate immediately.
    """
    response = await client.post(_OR_CHAT_URL, json=payload)
    response.raise_for_status()
    return response.json()


async def call_openrouter(
    prompt: str,
    model: str = _DEFAULT_MODEL,
    timeout: float = 1.5,
    price_multiplier: float = 1.0,
    redis=None,
) -> ProviderResponse:
    """
    Call the OpenRouter API and return a normalised ProviderResponse.

    Args:
        prompt:           Raw user prompt.
        model:            OpenRouter model slug (default: llama-3.3-70b-instruct).
        timeout:          TTFB timeout in seconds (PRD §3.9 SLA = 1.5s).
        price_multiplier: Dynamic pricing factor from Redis (Phase 4.1).

    Returns:
        ProviderResponse — always. Never raises.
    """
    start = time.perf_counter()

    model  = model or _DEFAULT_MODEL  # guard against empty-string from infer route
    payload = {
        "model":      model,
        "messages":   [{"role": "user", "content": prompt}],
        "max_tokens": _MAX_TOKENS,
    }

    effective_timeout = httpx.Timeout(connect=timeout, read=timeout, write=5.0, pool=1.0)

    try:
        async with httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "Content-Type":  "application/json",
                **_OR_EXTRA_HEADERS,
            },
            timeout=effective_timeout,
        ) as client:
            if redis:
                from api.chaos import inject_chaos
                await inject_chaos("openrouter", redis)
            data = await _openrouter_http_call(client, model, payload)

        latency_ms = int((time.perf_counter() - start) * 1000)

        output_text = data["choices"][0]["message"]["content"]
        usage       = data.get("usage", {})
        prompt_tokens     = usage.get("prompt_tokens",     0)
        completion_tokens = usage.get("completion_tokens", 0)
        total_tokens      = usage.get("total_tokens",      prompt_tokens + completion_tokens)
        actual_model      = data.get("model", model)

        cost_cents = calculate_cost_cents(
            prompt_tokens,
            completion_tokens,
            OR_INPUT_CENTS_PER_TOKEN  * price_multiplier,
            OR_OUTPUT_CENTS_PER_TOKEN * price_multiplier,
        )

        logger.debug(
            "OpenRouter OK — model=%s latency=%dms tokens=%d cost=%.4f¢",
            actual_model, latency_ms, total_tokens, cost_cents,
        )

        return ProviderResponse(
            latency_ms=latency_ms,
            token_count=total_tokens,
            cost_cents=cost_cents,
            error_flag=False,
            output_text=output_text,
            provider="openrouter",
            model=actual_model,
        )

    except httpx.TimeoutException as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        logger.warning("OpenRouter TIMEOUT after %dms: %s", latency_ms, exc)
        return ProviderResponse(latency_ms=latency_ms, error_flag=True, provider="openrouter", model=model)

    except httpx.HTTPStatusError as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        logger.warning("OpenRouter HTTP %d after %dms: %s", exc.response.status_code, latency_ms, exc)
        return ProviderResponse(latency_ms=latency_ms, error_flag=True, provider="openrouter", model=model)

    except Exception as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        logger.error("OpenRouter FAILED after %dms (%s): %s", latency_ms, type(exc).__name__, exc)
        return ProviderResponse(latency_ms=latency_ms, error_flag=True, provider="openrouter", model=model)
