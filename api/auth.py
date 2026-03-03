"""
api/auth.py — API Key Authentication (Phase 2.2)

Validates the `x-api-key` header on every Data Plane request.
Returns a derived `client_key` that rate_limiter.py and budget_guard.py
use as their Redis key suffix — so spend/rate data is tracked per unique
API key without storing the raw key anywhere in Redis.

Security properties:
  1. Timing-safe comparison via `hmac.compare_digest` — prevents timing
     attacks that could leak the secret key a bit at a time.
  2. Client key is SHA-256(api_key)[:32] hex — a deterministic, collision-
     resistant identifier that never exposes the raw key in Redis or logs.
  3. The raw `x-api-key` value is NEVER logged, stored in Postgres, or
     included in any Celery task payload (Phase 8.1 invariant).

FastAPI dependency chain for a protected route:
    verify_api_key()          → returns client_key (str)
        ↓ consumed by
    check_rate_limit()        → raises 429 if exhausted
    check_budget()            → raises 402 if over limit
    select_provider() + call  → the actual LLM request
"""

import hashlib
import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from api.config import get_settings

settings = get_settings()


def _derive_client_key(api_key: str) -> str:
    """
    Derive a stable, opaque client identifier from the raw API key.

    Uses the first 32 hex characters of SHA-256(api_key) — enough entropy
    to be collision-resistant while keeping Redis keys short.

    This means:
      • The same api_key always produces the same client_key (deterministic).
      • Two different api_keys never produce the same client_key (≈no collisions).
      • The raw api_key cannot be reconstructed from the client_key.
    """
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:32]


async def verify_api_key(
    x_api_key: Annotated[
        str,
        Header(
            alias="x-api-key",
            description="Client API key. Must match X_API_KEY_SECRET in .env.",
        ),
    ],
) -> str:
    """
    FastAPI dependency: validates the incoming `x-api-key` header.

    Returns:
        client_key (str) — SHA-256 derived identifier for this API key.
                           Passed downstream to rate_limiter and budget_guard.

    Raises:
        HTTP 403 FORBIDDEN — if the key does not match X_API_KEY_SECRET.
        HTTP 422 UNPROCESSABLE — if the x-api-key header is missing entirely
                                  (FastAPI raises this automatically for
                                  required Header parameters).

    Note: We raise 403 (Forbidden) rather than 401 (Unauthorized) because:
      • 401 implies the client can re-authenticate with different credentials.
      • 403 signals a definitive rejection — the key is simply wrong.
    """
    # hmac.compare_digest: constant-time string comparison.
    # A naive `==` leaks timing information (short-circuits on first mismatch)
    # which an attacker can exploit to brute-force the secret character by character.
    is_valid = hmac.compare_digest(
        x_api_key.encode("utf-8"),
        settings.x_api_key_secret.encode("utf-8"),
    )

    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            # Generic message — never reveal WHY the key was rejected.
            detail="Forbidden: invalid API key.",
        )

    return _derive_client_key(x_api_key)


# ---------------------------------------------------------------------------
# Annotated type alias — clean injection at call sites.
#
# Route signature becomes:
#   async def infer(client_key: ClientKeyDep, redis: RedisDep, ...):
# ---------------------------------------------------------------------------
ClientKeyDep = Annotated[str, Depends(verify_api_key)]
