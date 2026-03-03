"""
api/redis_keys.py — Redis Key Namespace Constants & Builder Functions
TODO.md §1.4: Every Redis key in the system MUST be built from these
              functions. No magic strings in business logic.

Design pattern:
  - Private `_TMPL_*` constants document the exact namespace schema.
  - Public builder functions (`key_*`) accept typed arguments and return
    the resolved key string. This prevents a whole class of silent bugs:
    if you typo a kwarg name in a raw `.format()` call, Python silently
    returns a key with the literal `{provider}` in it — a key that will
    never match any real entry in Redis.

All keys live in Redis DB 0 (REDIS_URL_MAB) except where noted.
Celery broker/result keys live in DB 1 (REDIS_URL_CELERY) and are managed
exclusively by the Celery library — never constructed here.

Cross-reference:
  tradeoffs-info §1: Celery must NEVER use REDIS_URL_MAB (DB 0).
  TODO §1.4: All Redis keys use namespaced constants — no inline strings.
"""

# ==============================================================================
# Namespace templates (private — call the builder functions below)
# ==============================================================================

# MAB (Multi-Armed Bandit) — DB 0
_TMPL_MAB_WEIGHTS      = "mab:weights:{provider}"
_TMPL_MAB_STATS_MU     = "mab:stats:{provider}:{metric}:mu"
_TMPL_MAB_STATS_VAR    = "mab:stats:{provider}:{metric}:var"

# L1 Exact-Match Prompt Cache — DB 0 (TTL: 86400s per PRD §3.9)
_TMPL_PROMPT_CACHE     = "cache:prompt:{prompt_hash}"

# Traffic Governance — DB 0
_TMPL_RATE_LIMIT       = "ratelimit:token:{client_key}"
_TMPL_DAILY_BUDGET     = "budget:daily:{client_key}"
_TMPL_BUDGET_BLOCKED   = "budget:blocked:{client_key}"

# Circuit Breaker — DB 0
_TMPL_CIRCUIT_STATE    = "circuit:{provider}:state"
_TMPL_CIRCUIT_FAILURES = "circuit:{provider}:failures"

# Inference Economics / Dynamic Pricing — DB 0 (TTL: 3600s = peak-hour window)
# Written by simulate_price_update Celery task (TODO §4.1).
# Read by api/providers/*.py when calculating cost_cents.
_TMPL_PRICE_MULTIPLIER = "pricing:multiplier:{provider}"

# Chaos Engine — DB 0 (TODO §4.2)
# Values: "none" | "slow" | "timeout" | "rate_limit"
_TMPL_CHAOS_MODE       = "chaos:{provider}:mode"

# Load Shedding Flag — DB 0 (TODO §4.3)
# A single key; presence + value of "1" means shed all new requests (HTTP 503).
LOAD_SHED_FLAG         = "system:load:shed_flag"


# ==============================================================================
# Valid provider identifiers (single source of truth — used by MAB + circuit)
# ==============================================================================

PROVIDERS = ("groq", "openrouter", "fallback")

# Valid EMA metric names (used in MAB stats keys)
METRICS = ("latency", "cost", "quality")

# Valid circuit breaker states
CIRCUIT_OPEN      = "OPEN"
CIRCUIT_CLOSED    = "CLOSED"
CIRCUIT_HALF_OPEN = "HALF_OPEN"


# ==============================================================================
# Key builder functions — USE THESE; never call .format() on templates directly
# ==============================================================================

def key_mab_weights(provider: str) -> str:
    """
    Redis key holding the current MAB reward weight for a provider (JSON float).
    Written by Celery update_mab_weights task (Phase 3.2).
    Read by select_provider() in the sync request loop (Phase 2.5).
    """
    _assert_provider(provider)
    return _TMPL_MAB_WEIGHTS.format(provider=provider)


def key_mab_stats_mu(provider: str, metric: str) -> str:
    """
    EMA running mean (μ) for a provider+metric pair.
    tradeoffs-info §2.1: μ_t = (1-λ)μ_{t-1} + λX_t
    """
    _assert_provider(provider)
    _assert_metric(metric)
    return _TMPL_MAB_STATS_MU.format(provider=provider, metric=metric)


def key_mab_stats_var(provider: str, metric: str) -> str:
    """
    EMA running variance (σ²) for a provider+metric pair.
    tradeoffs-info §2.1: σ²_t = (1-λ)σ²_{t-1} + λ(X_t - μ_t)²
    """
    _assert_provider(provider)
    _assert_metric(metric)
    return _TMPL_MAB_STATS_VAR.format(provider=provider, metric=metric)


def key_prompt_cache(prompt_hash: str) -> str:
    """
    L1 exact-match cache key for a prompt SHA-256 hex digest.
    TTL: 86400s (24h) per PRD §3.9.
    Value: JSON-serialised ProviderResponse.
    """
    if not prompt_hash or len(prompt_hash) != 64:  # SHA-256 hex = 64 chars
        raise ValueError(
            f"prompt_hash must be a 64-char SHA-256 hex digest, got: {prompt_hash!r}"
        )
    return _TMPL_PROMPT_CACHE.format(prompt_hash=prompt_hash)


def key_rate_limit(client_key: str) -> str:
    """
    Token bucket counter for per-client rate limiting (PRD §3.8).
    TTL: 60s (one sliding window). Value: remaining token count (int).
    """
    _assert_client_key(client_key)
    return _TMPL_RATE_LIMIT.format(client_key=client_key)


def key_daily_budget(client_key: str) -> str:
    """
    Daily spend accumulator in USD cents (PRD §3.8).
    Atomically incremented via INCRBYFLOAT by the deduct_budget Celery task.
    TTL: until midnight UTC.
    """
    _assert_client_key(client_key)
    return _TMPL_DAILY_BUDGET.format(client_key=client_key)


def key_budget_blocked(client_key: str) -> str:
    """
    Flag key set when a client exceeds their daily_limit_cents.
    Presence of this key (with value "1") causes HTTP 402 in budget_guard.
    TTL: until midnight UTC (same expiry as key_daily_budget).
    """
    _assert_client_key(client_key)
    return _TMPL_BUDGET_BLOCKED.format(client_key=client_key)


def key_circuit_state(provider: str) -> str:
    """
    Circuit breaker state for a provider.
    Value: CIRCUIT_OPEN | CIRCUIT_CLOSED | CIRCUIT_HALF_OPEN constants above.
    Read in the sync loop by is_open(); written as a background Celery task.
    """
    _assert_provider(provider)
    return _TMPL_CIRCUIT_STATE.format(provider=provider)


def key_circuit_failures(provider: str) -> str:
    """
    Rolling failure counter for a provider's circuit breaker.
    Incremented by record_failure() background task (Phase 2.7).
    """
    _assert_provider(provider)
    return _TMPL_CIRCUIT_FAILURES.format(provider=provider)


def key_price_multiplier(provider: str) -> str:
    """
    Dynamic cost multiplier for a provider (Phase 4.1 Inference Economics).
    Written every 15 min by simulate_price_update Celery Beat task.
    Read by api/providers/*.py when computing cost_cents.
    TTL: 3600s (one peak-hour window).
    Value: float multiplier, e.g. 1.6 means 60% price surge.
    """
    _assert_provider(provider)
    return _TMPL_PRICE_MULTIPLIER.format(provider=provider)


def key_chaos_mode(provider: str) -> str:
    """
    Chaos injection mode for a provider (Phase 4.2).
    Value: "none" | "slow" | "timeout" | "rate_limit"
    Set via POST /admin/chaos endpoint (auth-gated).
    """
    _assert_provider(provider)
    return _TMPL_CHAOS_MODE.format(provider=provider)


# ==============================================================================
# Internal validation guards (fail loudly, fail early)
# ==============================================================================

def _assert_provider(provider: str) -> None:
    if provider not in PROVIDERS:
        raise ValueError(
            f"Unknown provider {provider!r}. "
            f"Must be one of: {PROVIDERS}. "
            "If adding a new provider, update PROVIDERS in api/redis_keys.py first."
        )


def _assert_metric(metric: str) -> None:
    if metric not in METRICS:
        raise ValueError(
            f"Unknown metric {metric!r}. Must be one of: {METRICS}."
        )


def _assert_client_key(client_key: str) -> None:
    if not client_key or not isinstance(client_key, str):
        raise ValueError("client_key must be a non-empty string.")