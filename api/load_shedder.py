"""
api/load_shedder.py — Progressive Load Shedding (Phase 4.3)

Provides a lightweight, zero-dependency mechanism to reject all incoming
requests instantly (HTTP 503) if the system is globally overwhelmed.
The flag is read directly from Redis at the very top of the inference route.
"""

from api.redis_keys import LOAD_SHED_FLAG

async def should_shed(redis) -> bool:
    """
    Checks if the global load shedding flag is set in Redis.
    If '1', the system should immediately reject new traffic.
    """
    flag = await redis.get(LOAD_SHED_FLAG)
    return flag == "1"
