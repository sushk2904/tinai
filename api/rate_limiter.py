"""
api/rate_limiter.py — Fixed-Window Rate Limiter (Phase 2.2)

Implements a per-client fixed-window counter using Redis INCR + EXPIRE.

Why fixed-window (not token bucket):
  • Token bucket needs two Redis round-trips and floating-point state.
  • Fixed-window needs one atomic INCR — O(1) and fits in the hot path.
  • The burst edge-case (2× throughput at window boundaries) is acceptable
    for this system; Phase 8.1 can graduate to a sliding-window Lua script.

Implementation — atomic Lua script:
  We use a single Lua script executed via Redis EVAL. This is critical:
  if we split INCR and EXPIRE into two round-trips, a process crash between
  them could leave a key with no TTL that grows forever. The Lua script
  executes atomically — either both succeed or neither does.

Redis key:  ratelimit:token:{client_key}  (DB 0)
TTL:        rate_limit_window_seconds (default 60s)
Value:      integer counter of requests in current window
Threshold:  rate_limit_requests (default 100 req/window)

Called in the sync request loop — must be O(1) and non-blocking.
"""

from redis.asyncio import Redis
from fastapi import HTTPException, status

from api.config import get_settings
from api.redis_keys import key_rate_limit

settings = get_settings()

# ---------------------------------------------------------------------------
# Lua script — atomically INCR the counter and set TTL only on first request.
#
# KEYS[1]  = the rate-limit key for this client
# ARGV[1]  = window TTL in seconds
#
# Returns the current count after increment.
#
# Why `redis.call('TTL', KEYS[1]) == -1`:
#   TTL returns -1 when the key exists but has no expiry.
#   We only set EXPIRE when there is no TTL — this prevents resetting
#   the window on every request (which would give infinite capacity).
#   On the very first request in a window the key does not exist yet,
#   so INCR creates it with no TTL, then we immediately set EXPIRE.
# ---------------------------------------------------------------------------
_RATE_LIMIT_LUA = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return current
"""


async def check_rate_limit(client_key: str, redis: Redis) -> None:
    """
    Enforce the fixed-window rate limit for `client_key`.

    Reads `ratelimit:token:{client_key}` from Redis DB 0 and increments it
    atomically. Raises HTTP 429 if the count exceeds the configured threshold.

    Args:
        client_key: SHA-256 derived key from auth.verify_api_key().
        redis:      The shared Redis client from get_redis() dependency.

    Raises:
        HTTP 429 TOO MANY REQUESTS — with `Retry-After` header set to the
        number of seconds remaining in the current window.

    Called from:
        api/routers/infer.py — step 2 of the sync request loop (Phase 2.6).
    """
    rkey = key_rate_limit(client_key)

    # Execute the Lua script: single round-trip, fully atomic.
    count = await redis.eval(
        _RATE_LIMIT_LUA,
        1,                                          # numkeys
        rkey,                                       # KEYS[1]
        str(settings.rate_limit_window_seconds),    # ARGV[1]
    )

    if count > settings.rate_limit_requests:
        # Fetch remaining TTL so the client knows when to retry.
        ttl = await redis.ttl(rkey)
        retry_after = max(ttl, 1)  # never return 0 or negative

        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Rate limit exceeded: {settings.rate_limit_requests} requests "
                f"per {settings.rate_limit_window_seconds}s window."
            ),
            headers={"Retry-After": str(retry_after)},
        )
