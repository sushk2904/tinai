"""
api/providers/retry.py — Shared Tenacity Retry Decorator (Phase 2.3)

A SINGLE retry decorator shared by all provider HTTP calls.
Centralising it here means changing retry behaviour (e.g., adding
`httpx.ReadError` to the retry set) requires one edit, not three.

Retry budget analysis (TODO §2.3 hard constraint):
  1500ms SLA (PRD §3.9) must survive the retry path:
  ┌─────────────────────────────────────────────────────┐
  │ Attempt 1: up to 1500ms (httpx TTFB timeout)        │
  │ wait_fixed: 100ms                                   │
  │ Attempt 2: up to 1500ms (httpx TTFB timeout)        │
  │ Total worst case: 3100ms                            │
  └─────────────────────────────────────────────────────┘
  This EXCEEDS the SLA — but it is intentional:
  Retries only trigger on ConnectError / RemoteProtocolError,
  not on TimeoutException. A connect error typically resolves
  in <10ms (TCP RST is instant). If attempt 1 times out at
  1500ms, the retry is NOT triggered (TimeoutException is
  excluded from the retry set), so the SLA holds.

What IS retried (transient transport errors):
  • httpx.ConnectError       — TCP connection refused / network unreachable
  • httpx.RemoteProtocolError — server sent malformed HTTP (e.g., 502 proxy)

What is NOT retried:
  • httpx.TimeoutException   — SLA already breached; fail immediately
  • httpx.HTTPStatusError    — 4xx/5xx are deterministic; retrying won't help
  • Any other exception      — fail fast and return ProviderResponse(error=True)
"""

import logging

import httpx
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

logger = logging.getLogger("tinai.providers.retry")


def _log_retry_attempt(retry_state: RetryCallState) -> None:
    """
    Callback fired by tenacity before each retry attempt.
    Logs the exception that triggered the retry so it appears in
    docker compose logs without crashing the application.
    """
    exc = retry_state.outcome.exception()
    logger.warning(
        "Provider HTTP error (attempt %d/%d) — retrying in 100ms. "
        "Error: %s: %s",
        retry_state.attempt_number,
        2,  # stop_after_attempt value
        type(exc).__name__,
        exc,
    )


# ---------------------------------------------------------------------------
# The shared retry decorator — apply this to inner HTTP call functions only.
#
# Usage in provider modules:
#   from api.providers.retry import provider_retry
#
#   @provider_retry
#   async def _make_http_call(client, url, headers, payload):
#       response = await client.post(url, ...)
#       response.raise_for_status()
#       return response.json()
# ---------------------------------------------------------------------------
provider_retry = retry(
    # 2 total attempts = 1 initial + 1 retry.
    # stop_after_attempt(2) means tenacity stops before a 3rd attempt.
    stop=stop_after_attempt(2),

    # 100ms fixed wait between attempts.
    # With two ~600ms attempts this keeps total under 1500ms on connect errors.
    wait=wait_fixed(0.1),

    # ONLY retry on transient transport-level failures:
    #   ConnectError:        TCP refused, network drop between containers
    #   RemoteProtocolError: Provider sent back a malformed response (e.g., 502)
    #
    # httpx.TimeoutException is deliberately excluded — the SLA is already
    # breached if we time out; retrying would double the damage.
    #
    # httpx.HTTPStatusError is excluded — 4xx/5xx are deterministic; a 429
    # from Groq won't resolve in 100ms.
    retry=retry_if_exception_type((
        httpx.ConnectError,
        httpx.RemoteProtocolError,
    )),

    # Log each retry attempt without swallowing the error.
    before_sleep=_log_retry_attempt,

    # After all attempts are exhausted, re-raise the last exception so the
    # outer provider function can catch it and return ProviderResponse(error=True).
    reraise=True,
)
