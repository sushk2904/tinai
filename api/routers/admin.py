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
from api.dependencies import RedisDep
from api.redis_keys import PROVIDERS, LOAD_SHED_FLAG, key_chaos_mode

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

class LoadShedRequest(BaseModel):
    active: bool = Field(..., description="True to block all traffic, False to resume.")

@router.post("/load-shedding", summary="Toggle Progressive Load Shedding")
async def toggle_load_shedding(body: LoadShedRequest, redis: RedisDep):
    """
    Toggles the global load shedding switch in Redis.
    If active, the API will aggressively reject all new inference requests with an HTTP 503.
    """
    if body.active:
        # 1 hour expiration so it auto-recovers if we forget
        await redis.set(LOAD_SHED_FLAG, "1", ex=3600)
    else:
        await redis.delete(LOAD_SHED_FLAG)
    
    return {
        "status": "success",
        "load_shedding_active": body.active,
        "message": f"Global Load Shedding set to {body.active}"
    }
