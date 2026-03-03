"""
api/config.py — Pydantic Settings (Phase 2.1)

All environment variables are declared here with explicit Python types.
pydantic-settings v2 reads them from:
  1. Environment variables (highest priority — what Docker Compose injects)
  2. .env file (fallback for local dev without `docker compose`)

Why pydantic==2.10.6 + pydantic-settings==2.7.1 (TODO §2.1 constraint):
  The pre-compiled Rust wheel ships with the v2 core. On Python 3.11 this
  avoids the overhead of re-compiling pydantic-core and gives us the ~5–10x
  validation speedup over v1 — critical at 200–500 RPS burst.

Usage:
    from api.config import settings
    url = settings.redis_url_mab
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Type-safe, validated mapping of every .env variable the application uses.
    All fields are read-only after instantiation (model_config frozen=True).
    """

    model_config = SettingsConfigDict(
        # Load .env for local dev (docker compose always injects real env vars
        # so the file is only used as a fallback during bare `uvicorn` runs).
        env_file=".env",
        env_file_encoding="utf-8",
        # Silently ignore extra environment variables present in the shell
        # (e.g., PATH, HOME) — we only care about TINAI-specific vars.
        extra="ignore",
        # Freeze the instance so settings can't be mutated after startup.
        frozen=True,
    )

    # -------------------------------------------------------------------------
    # PostgreSQL
    # -------------------------------------------------------------------------
    postgres_user:     str = Field(..., description="Postgres superuser name")
    postgres_password: str = Field(..., description="Postgres password")
    postgres_db:       str = Field(..., description="Postgres database name")
    postgres_host:     str = Field("postgres", description="Postgres hostname (service name on tinai_net)")
    postgres_port:     int = Field(5432, description="Postgres port")

    # asyncpg DSN used by the FastAPI runtime connection pool.
    # Must use postgresql+asyncpg:// driver prefix for SQLAlchemy-style URLs,
    # or plain postgresql:// for raw asyncpg.create_pool().
    database_url: str = Field(
        ...,
        description="Full asyncpg DSN — postgresql+asyncpg://user:pass@host/db",
    )

    # Synchronous DSN for Alembic (psycopg2 driver). Never used at runtime.
    alembic_database_url: str = Field(
        ...,
        description="Synchronous DSN for Alembic — postgresql://user:pass@host/db",
    )

    # -------------------------------------------------------------------------
    # Redis — key-space isolation (tradeoffs-info §1)
    # DB 0 → MAB weights, L1 cache, rate-limit, circuit-breakers (REDIS_URL_MAB)
    # DB 1 → Celery broker + Beat schedule         (REDIS_URL_CELERY)
    # These MUST target different DB numbers. test_infra.py validates isolation.
    # -------------------------------------------------------------------------
    redis_url_mab:    str = Field("redis://redis:6379/0", description="Redis DB 0 — control plane")
    redis_url_celery: str = Field("redis://redis:6379/1", description="Redis DB 1 — Celery broker")

    # -------------------------------------------------------------------------
    # LLM Provider API Keys (external HTTP calls only — no local models)
    # -------------------------------------------------------------------------
    groq_api_key:        str = Field(..., description="Groq API key")
    openrouter_api_key:  str = Field(..., description="OpenRouter API key")
    fallback_api_key:    str = Field(..., description="Fallback model API key")
    fallback_model:      str = Field("llama-3.3-70b-versatile", description="Fallback model ID")
    fallback_base_url:   str = Field("https://api.groq.com/openai/v1", description="Fallback provider base URL")

    # -------------------------------------------------------------------------
    # Observability (always Celery tasks — never inline per tradeoffs-info §1)
    # -------------------------------------------------------------------------
    langfuse_secret_key: str = Field("", description="Langfuse secret key")
    langfuse_public_key: str = Field("", description="Langfuse public key")
    langfuse_host:       str = Field("https://cloud.langfuse.com")

    arize_api_key:   str = Field("", description="Arize API key")
    arize_space_key: str = Field("", description="Arize space key")

    # -------------------------------------------------------------------------
    # API Security — Traffic Governance (PRD §3.8)
    # -------------------------------------------------------------------------
    x_api_key_secret: str = Field(
        ...,
        description="Shared secret checked against x-api-key header on all /v1/* routes",
    )

    # -------------------------------------------------------------------------
    # Runtime behaviour flags
    # -------------------------------------------------------------------------
    environment: Literal["dev", "prod"] = Field(
        "dev",
        description="dev → chaos endpoints enabled | prod → disabled, strict limits",
    )

    # Fraction of requests sampled for hallucination/safety checking (Phase 3.3).
    safety_sample_rate: float = Field(
        0.10,
        ge=0.0,
        le=1.0,
        description="0.0–1.0 fraction of requests sent to the safety proxy task",
    )

    # -------------------------------------------------------------------------
    # MAB reward function weights (tradeoffs-info §2.3: R = α·Zq - β·Zl - γ·Zc)
    # -------------------------------------------------------------------------
    mab_alpha:      float = Field(1.0, ge=0.0, description="Quality weight α")
    mab_beta:       float = Field(0.5, ge=0.0, description="Latency weight β")
    mab_gamma:      float = Field(0.5, ge=0.0, description="Cost weight γ")

    # EMA decay factor λ (tradeoffs-info §2.1). Changing this invalidates all
    # running EMA stats in Redis — flush DB 0 if you change this in prod.
    mab_ema_lambda: float = Field(0.1, gt=0.0, lt=1.0, description="EMA decay λ")

    # -------------------------------------------------------------------------
    # Performance SLAs (PRD §3.9)
    # -------------------------------------------------------------------------
    # Hard TTFB timeout for external LLM calls. PRD §3.9: abort at 1500ms.
    llm_timeout_seconds: float = Field(1.5, gt=0.0, description="Provider HTTP timeout (s)")

    # L1 prompt cache TTL in seconds (PRD §3.9: 24 hours = 86400s).
    cache_ttl_seconds: int = Field(86400, gt=0, description="L1 cache TTL (s)")

    # Per-client daily budget ceiling in USD cents (PRD §3.8).
    # tradeoffs-info §3: all spend tracking in cents, never raw USD floats.
    default_daily_budget_cents: float = Field(
        10000.0,
        ge=0.0,
        description="Default per-client daily budget ceiling (USD cents)",
    )

    # -------------------------------------------------------------------------
    # Rate limiting — fixed-window counter (Phase 2.2)
    # `rate_limit_requests` tokens are allowed per `rate_limit_window_seconds`
    # window per client key. Enforced by api/rate_limiter.py via Redis INCR.
    # -------------------------------------------------------------------------
    rate_limit_requests:       int = Field(100, ge=1, description="Max requests per window")
    rate_limit_window_seconds: int = Field(60,  ge=1, description="Rate-limit window (s)")

    # -------------------------------------------------------------------------
    # asyncpg Connection Pool sizing
    # At 200–500 RPS burst with 4 Uvicorn workers, the pool needs headroom.
    # Rule of thumb: min = 1 per worker, max = 5 per worker (20 total for 4 workers).
    # -------------------------------------------------------------------------
    db_pool_min_size: int = Field(5,  ge=1,  description="asyncpg pool min connections")
    db_pool_max_size: int = Field(20, ge=1,  description="asyncpg pool max connections")

    # -------------------------------------------------------------------------
    # Validators
    # -------------------------------------------------------------------------
    @field_validator("database_url")
    @classmethod
    def _database_url_uses_async_driver(cls, v: str) -> str:
        """
        The runtime pool uses asyncpg. Alembic uses psycopg2 (alembic_database_url).
        This validator catches a common mistake: using a sync postgresql:// URL
        in DATABASE_URL, which would fail silently at connection pool creation.
        """
        if v.startswith("postgresql://") and "+asyncpg" not in v:
            # Auto-correct: strip the bare postgresql:// and add asyncpg driver.
            return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    @field_validator("db_pool_max_size")
    @classmethod
    def _pool_max_gte_min(cls, v: int, info) -> int:
        min_size = info.data.get("db_pool_min_size", 5)
        if v < min_size:
            raise ValueError(
                f"db_pool_max_size ({v}) must be >= db_pool_min_size ({min_size})"
            )
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return the application Settings singleton.

    @lru_cache(maxsize=1) ensures Settings() is instantiated exactly once per
    process lifetime — no repeated .env file reads on every request. The cache
    is cleared between tests by calling get_settings.cache_clear().

    Usage in FastAPI routes:
        from api.config import get_settings
        settings = get_settings()

    Usage in dependencies.py (preferred — avoids module-level instantiation):
        Depends(get_settings)
    """
    return Settings()


# ---------------------------------------------------------------------------
# Module-level singleton for non-dependency-injection contexts
# (e.g., Celery tasks, alembic env.py, one-shot scripts).
# ---------------------------------------------------------------------------
settings: Settings = get_settings()
