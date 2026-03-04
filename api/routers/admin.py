"""
api/routers/admin.py — Administrative & Debug Endpoints (Phase 4.2)

Provides endpoints to manually interact with the system's dynamic configuration
and simulated environment features (like the Chaos Engine). These endpoints
are auth-guarded with the same x-api-key as the data plane.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.auth import verify_api_key
from api.chaos import ChaosMode
from api.redis_client import RedisDep
from api.redis_keys import PROVIDERS, key_chaos_mode

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(verify_api_key)],
)


class ChaosRequest(BaseModel):
    provider: str = Field(..., description=f"Must be one of: {PROVIDERS}")
    mode: ChaosMode = Field(..., description="The failure scenario to inject.")


@router.post("/chaos", summary="Inject Chaos for a Provider")
async def inject_chaos_endpoint(body: ChaosRequest, redis: RedisDep):
    """
    Sets the chaos mode flag for a specific provider in Redis.
    Used to verify Circuit Breaker and MAB routing robustness during gray or hard failures.
    
    Modes:
      - none:       Normal operation.
      - slow:       Injects 1.0 - 3.0s latency (gray failure).
      - timeout:    Injects latency exceeding the SLA then raises TimeoutException.
      - rate_limit: Immediately raises HTTP 429.
    """
    if body.provider not in PROVIDERS:
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid provider {body.provider!r}. Must be one of: {PROVIDERS}"
        )

    rkey = key_chaos_mode(body.provider)
    
    if body.mode == ChaosMode.NONE:
        # Clean up the key if returning to normal
        await redis.delete(rkey)
    else:
        # Persist the chaos mode. Chaos persists until reverted or Redis eviction.
        # We set a 1h TTL just to be safe if someone forgets to turn it off.
        await redis.set(rkey, body.mode.value, ex=3600)

    return {
        "status": "success",
        "provider": body.provider,
        "mode": body.mode.value,
        "message": f"Chaos mode '{body.mode.value}' active for {body.provider}."
    }
