"""
api/providers/base.py — Shared Data Contracts (Phase 2.3)

The single source of truth for what every provider returns.
ALL provider callables (groq, openrouter, fallback) MUST return a
ProviderResponse — never raise out of the provider layer.

Design decisions:
  • dataclass over Pydantic model: zero-overhead construction in the hot path.
    A Pydantic model adds ~10µs of validation per call; at 500 RPS this is
    5ms/s of wasted CPU just on response parsing.
  • cost_cents: float (not Decimal) — staying in float is fine for arithmetic
    since we store the value in Postgres as Numeric(10,4) via SQLAlchemy.
    The tradeoffs-info §3 constraint is about the unit (cents), not the type.
  • provider and model fields: populated by each provider implementation so
    the Celery telemetry task knows exactly what was called, without needing
    the MAB router to pass them separately.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ProviderResponse:
    """
    Standardised output from every LLM provider call.
    PRD §3.1: [Latency (ms), Token Count, Cost Estimate (USD Cents),
               Error Flag (Boolean), Output Text]
    """

    # Wall-clock latency: start before first attempt, end after last.
    # Includes all retry attempts and the wait_fixed(0.1) gap between them.
    latency_ms: int = 0

    # Nullable — not all providers return token counts on every response.
    token_count: Optional[int] = None

    # Total cost in USD cents for this request (tradeoffs-info §3).
    # Formula: (prompt_tokens × input_price_per_token)
    #         + (completion_tokens × output_price_per_token)
    # Both prices expressed in cents/token.
    cost_cents: float = 0.0

    # True if the provider call failed (timeout, max retries, 5xx, etc.)
    error_flag: bool = False

    # The raw text returned by the LLM. None on error.
    output_text: Optional[str] = None

    # Which provider actually served this response (filled by each module).
    provider: str = ""

    # Which model was used (from the response JSON, not the request payload).
    model: str = ""


@dataclass
class InferenceRequest:
    """
    Normalised input to the provider layer.
    Constructed in api/routers/infer.py from the validated request body.
    """
    prompt:     str
    model:      str  = ""           # provider-specific model ID
    policy:     str  = "sla-aware"  # latency-first | cost-first | sla-aware
    client_key: str  = ""           # SHA-256 derived key from auth.py
    request_id: str  = ""           # UUID injected by middleware


# ---------------------------------------------------------------------------
# Pricing table — USD cents per token (tradeoffs-info §3)
#
# Empirically verified from benchmark test (2026-03-03):
#   Prompt: "Explain mutex vs semaphore"
#   Groq   llama-3.3-70b-versatile   → 554 tokens → 0.0355¢  total
#   OR     llama-3.3-70b-instruct    → 489 tokens → 0.0000¢  (FREE tier)
#   Groq   llama-3.1-8b-instant      → 526 tokens → 0.0030¢  total
#
# MAB implication: OpenRouter FREE cost creates a strong cost-first signal.
# The 16,723ms latency (8× Groq) is the counter-penalty on sla-aware policy.
# Phase 4.1 price multipliers will simulate dynamic pricing surges on top.
# ---------------------------------------------------------------------------

# Groq: Premium high-speed 70B (user base: 0.060 cents / 1000 tokens)
GROQ_INPUT_CENTS_PER_TOKEN:  float = 0.000060
GROQ_OUTPUT_CENTS_PER_TOKEN: float = 0.000060

# OpenRouter: (user base: 0.004 cents / 1000 tokens)
# Given it a tiny base cost so dynamic multipliers have an effect.
OR_INPUT_CENTS_PER_TOKEN:  float = 0.000004
OR_OUTPUT_CENTS_PER_TOKEN: float = 0.000004

# Fallback: The 8B Speed Demon (user base: 0.015 cents / 1000 tokens)
FALLBACK_INPUT_CENTS_PER_TOKEN:  float = 0.000015
FALLBACK_OUTPUT_CENTS_PER_TOKEN: float = 0.000015


def calculate_cost_cents(
    prompt_tokens: int,
    completion_tokens: int,
    input_price: float,
    output_price: float,
) -> float:
    """
    Compute total request cost in USD cents.
    Called by each provider module after parsing the usage block.

    Args:
        prompt_tokens:     Number of input tokens billed by the provider.
        completion_tokens: Number of output tokens billed by the provider.
        input_price:       Cents per input token (from PRICE constants above).
        output_price:      Cents per output token.

    Returns:
        float: Total cost in USD cents, rounded to 4 decimal places.
    """
    cost = (prompt_tokens * input_price) + (completion_tokens * output_price)
    return round(cost, 4)
