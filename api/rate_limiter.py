# ---------------------------------------------------------
# SECURITY: SLIDING-WINDOW RATE LIMITER CONFIGURATION
# Engineered for 1M+ Daily Requests (11.5 RPS avg, 500 RPS burst)
# ---------------------------------------------------------
import time
from redis.asyncio import Redis
from fastapi import HTTPException, status

from api.config import get_settings
from api.redis_keys import key_rate_limit

settings = get_settings()

# ---------------------------------------------------------------------------
# Lua script — Sliding-Window Counter (Mathematical Approximation)
#
# Formula: Count = CurrentCount + (PreviousCount * (WindowSize - Overlap) / WindowSize)
#
# KEYS[1]  = the base rate-limit key for this client (perf:v2:ratelimit:{client_key})
# ARGV[1]  = current timestamp (float seconds)
# ARGV[2]  = window TTL in seconds
#
# Returns the weighted count (float).
# ---------------------------------------------------------------------------
_SLIDING_WINDOW_LUA = """
local now = tonumber(ARGV[1])
local window_sec = tonumber(ARGV[2])
local current_bucket = math.floor(now / window_sec)
local prev_bucket = current_bucket - 1

local current_key = KEYS[1] .. ":" .. current_bucket
local prev_key = KEYS[1] .. ":" .. prev_bucket

-- Increment current bucket
local current_count = redis.call('INCR', current_key)
if current_count == 1 then
    redis.call('EXPIRE', current_key, window_sec * 2)
end

-- Fetch previous bucket
local prev_count = tonumber(redis.call('GET', prev_key) or 0)

-- Calculate overlap weight
local weight = (window_sec - (now % window_sec)) / window_sec
local total = current_count + (prev_count * weight)

return tostring(total)
"""


async def check_rate_limit(client_key: str, redis: Redis) -> None:
    """
    Enforce a sliding-window rate limit for `client_key`.

    Uses the mathematical approximation formula:
    Count = requests_in_current_fixed_window +
            (requests_in_previous_fixed_window * portion_of_previous_window_overlap)

    This prevents the "burst at boundary" issue of fixed windows while
    maintaining O(1) Redis performance.
    """
    base_key = f"ratelimit:v2:{client_key}"
    now = time.time()

    # Execute the Lua script
    weighted_count_str = await redis.eval(
        _SLIDING_WINDOW_LUA,
        1,
        base_key,
        str(now),
        str(settings.rate_limit_window_seconds),
    )
    
    count = float(weighted_count_str)

    if count > settings.rate_limit_requests:
        # For sliding window, we approximate retry-after as the time until
        # the current count would drop below the limit. Simplest is to wait
        # until the start of the next window.
        retry_after = settings.rate_limit_window_seconds - (int(now) % settings.rate_limit_window_seconds)

        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Rate limit exceeded: {settings.rate_limit_requests} requests "
                f"per {settings.rate_limit_window_seconds}s sliding window."
            ),
            headers={"Retry-After": str(retry_after)},
        )
