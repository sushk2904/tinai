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
# Hyper-inflated for the 37-Minute High-Impact Portfolio Demo
# Formula: $/1k tokens / 1000 * 100 = cents/token
# ---------------------------------------------------------------------------

# Reference mapping for the demo orchestration (Unit: $/1k tokens)
PROVIDER_COST_MULTIPLIERS = {
    "openrouter": (0.00, 0.00),     # The "Free Tier" Bait
    "groq":       (0.02, 0.05),     # Standard Production
    "fallback":   (2.00, 4.00)      # The Financial Bleed (SambaNova)
}

# Values below are in USD Cents per Token

# Groq: Standard Production ($0.02 - $0.05 / 1k tokens)
GROQ_INPUT_CENTS_PER_TOKEN:  float = 0.0020
GROQ_OUTPUT_CENTS_PER_TOKEN: float = 0.0050

# OpenRouter: The "Free Tier" Bait ($0.00 / 1k tokens)
OR_INPUT_CENTS_PER_TOKEN:  float = 0.0000
OR_OUTPUT_CENTS_PER_TOKEN: float = 0.0000

# Fallback: The Financial Bleed ($2.00 - $4.00 / 1k tokens)
FALLBACK_INPUT_CENTS_PER_TOKEN:  float = 0.2000
FALLBACK_OUTPUT_CENTS_PER_TOKEN: float = 0.4000


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
