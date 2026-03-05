# TINAI — Master Execution TODO
> **Architecture laws live in `tradeoffs-info.md`. This file is the execution plan.**
> Phases are strictly sequential. Do not begin a phase until every checkbox in the prior phase is checked.

---

## LEGEND
- `[CP]` — Control Plane concern (Redis / FastAPI routing logic)
- `[DP]` — Data Plane concern (FastAPI sync request loop)
- `[RL]` — Reliability Layer concern (Celery async workers — NEVER called in the sync loop)
- `[OB]` — Observability concern (Langfuse / Arize, always non-blocking background tasks)
- `[DB]` — PostgreSQL schema / persistence concern
- `[DA]` — Dashboard / Streamlit concern

---

## PHASE 1 — Infrastructure Bootstrapping (Docker, Postgres, Redis)
> **Hard Rule:** Zero application logic is written until every container is healthy and inter-service networking is verified.

### 1.1 Repo & Project Scaffold
- [ ] Create top-level `docker-compose.yml` with services: `api`, `worker`, `redis`, `postgres`, `dashboard`
- [ ] Create `.env.example` with variables: `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`,
  - `REDIS_URL_MAB=redis://redis:6379/0` — **dedicated DB for MAB weights, L1 cache, rate-limit, circuit-breaker keys** (must be isolated from Celery)
  - `REDIS_URL_CELERY=redis://redis:6379/1` — **dedicated DB for Celery broker queue and Celery Beat schedule** (prevents Celery queue backpressure from evicting MAB keys under `allkeys-lru`)
  - `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `FALLBACK_API_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_PUBLIC_KEY`, `ARIZE_API_KEY`, `X_API_KEY_SECRET`
- [ ] Copy `.env.example` → `.env` and fill in local dev values (never commit `.env`)
- [ ] Add `.env` and `__pycache__/` to `.gitignore`
- [ ] Create project directory tree:
  ```
  tinai/
  ├── api/           # FastAPI app
  ├── workers/       # Celery tasks
  ├── dashboard/     # Streamlit app
  ├── migrations/    # Alembic env + versioned migration scripts
  ├── tests/         # pytest + k6 scripts
  └── docker/        # Per-service Dockerfiles
  ```
- [ ] Create `docker/Dockerfile.api` — Python 3.11-slim base, install `requirements.txt`
- [ ] Create `docker/Dockerfile.worker` — same base as API, entrypoint: `celery -A workers.celery_app worker`
- [ ] Create `docker/Dockerfile.dashboard` — Python 3.11-slim, install `requirements-dashboard.txt`

### 1.2 Docker Compose — Service Definitions
- [ ] Define `postgres` service: image `postgres:16`, expose port `5432`, env vars from `.env` (schema is applied by Alembic, **not** by mounting an init.sql — removing that mount prevents silent schema drift)
- [ ] Define `redis` service: image `redis:7-alpine`, expose port `6379`, add `--maxmemory 256mb --maxmemory-policy allkeys-lru`
- [ ] Define `api` service: build from `docker/Dockerfile.api`, depends_on `postgres` and `redis`, expose port `8000`
- [ ] Define `worker` service: build from `docker/Dockerfile.worker`, depends_on `redis` and `postgres`, same env as `api`
- [ ] Define `dashboard` service: build from `docker/Dockerfile.dashboard`, expose port `8501`
- [ ] Add a shared Docker network (`tinai_net`) so all services communicate by hostname
- [ ] Define 'beat' service: build from 'docker/Dockerfile.worker', command: 'celery -A workers.celery_app beat', depends_on 'redis'

### 1.3 PostgreSQL Schema via Alembic (`[DB]`)
> **Why Alembic, not init.sql:** Manually executing a raw `.sql` file gives you no version history. When you add a column next week, you will have no record of what state the DB is in across dev/prod. Alembic solves this with numbered versioned migrations.

- [ ] Add `alembic`, `sqlalchemy` to `requirements.txt`
- [ ] Run `alembic init migrations` inside the `api/` container to scaffold `migrations/env.py` and `alembic.ini`
- [ ] Configure `alembic.ini` `sqlalchemy.url` to read from `DATABASE_URL` env var (never hardcode credentials)
- [ ] Create `api/models.py` — define SQLAlchemy `Base` and all ORM models:
  - [ ] `InferenceLog` — columns: `id (BigInteger PK)`, `request_id (UUID)`, `client_key`, `provider (Text, index=True)`, `policy`, `prompt_hash`, `latency_ms (Integer)`, `token_count (Integer, nullable)`, `cost_cents (Numeric 10,4)` *(USD cents, per tradeoffs-info §3)*, `error_flag (Boolean, default False)`, `output_text (Text, nullable)`, `created_at (TIMESTAMPTZ, server_default=now(), index=True)`
    > **⚠️ Sequential Scan Death Trap:** At 1M rows/day, the Phase 3.4 drift query (`WHERE created_at >= NOW() - INTERVAL '24h' AND provider = ?`) will full-scan the entire table without these indexes — exhausting Neon free-tier compute credits within 3 days. `index=True` on both columns causes Alembic to emit `CREATE INDEX` statements automatically in the migration.
  - [ ] `ProviderStats` — columns: `provider (Text PK)`, `ema_latency_mu`, `ema_latency_var`, `ema_cost_mu`, `ema_cost_var`, `ema_quality_mu`, `ema_quality_var (all Numeric 12,6)`, `updated_at (TIMESTAMPTZ)`
  - [ ] `DriftSnapshot` — columns: `id (BigInteger PK)`, `run_date (Date)`, `provider (Text)`, `drift_score (Numeric 6,4)`, `sample_count (Integer)`, `created_at (TIMESTAMPTZ)`
  - [ ] `ClientBudget` — columns: `client_key (Text PK)`, `daily_limit_cents (Numeric 10,4, default 10000)`, `spent_today_cents (Numeric 10,4, default 0)`, `reset_at (TIMESTAMPTZ)`
- [ ] Run `alembic revision --autogenerate -m "initial_schema"` to auto-generate the first migration from the models
- [ ] Inspect the generated file in `migrations/versions/` — verify column types match the spec above
- [ ] Run `alembic upgrade head` to apply the migration to the local Postgres container
- [ ] Verify tables exist: `docker compose exec postgres psql -U $POSTGRES_USER -d $POSTGRES_DB -c "\dt"`
- [ ] For every future schema change: create a new `alembic revision --autogenerate` — **never edit the initial migration**

### 1.4 Redis Key Namespace Design (`[CP]`)
- [ ] Document Redis key schema in `api/redis_keys.py` as constants (no magic strings):
  - `mab:weights:{provider}` — MAB Z-score reward weight (JSON blob)
  - `mab:stats:{provider}:{metric}:mu` / `:var` — EMA running stats
  - `cache:prompt:{sha256_hash}` — L1 exact-match cache (TTL 86400s)
  - `ratelimit:token:{client_key}` — token bucket counter (TTL 60s)
  - `budget:daily:{client_key}` — daily spend in cents (TTL until midnight)
  - `circuit:{provider}:state` — `CLOSED` | `OPEN` | `HALF_OPEN`
  - `circuit:{provider}:failures` — rolling failure counter

### 1.5 Connectivity Verification
- [ ] Run `docker compose up -d postgres redis`
- [ ] Verify Postgres is healthy: `docker compose exec postgres psql -U $POSTGRES_USER -c "\dt"`
- [ ] Apply schema via Alembic: `docker compose exec api alembic upgrade head` (not via raw SQL file)
- [ ] Verify Redis: `docker compose exec redis redis-cli PING` → expect `PONG`
- [ ] Write a one-shot `tests/test_infra.py` that asserts Postgres and Redis are reachable from the `api` container network
- [ ] Run `docker compose up api` and confirm Python can connect to both services (no app logic yet, just health dependencies)

---

## PHASE 2 — Core Data Plane (FastAPI — Synchronous Path Only)
> **Strict Rule (tradeoffs-info §1):** The sync request loop may ONLY: read MAB weights from Redis, call the external LLM API, return the response. Everything else is forbidden here.

### 2.1 FastAPI App Skeleton (`[DP]`)
- [ ] Install dependencies into `requirements.txt`: `fastapi`, `uvicorn[standard]`, `httpx`, `redis[asyncio]`, `asyncpg`, `pydantic==2.10.6` (pre-compiled V2 Rust wheel — avoids recompile issues on Windows and is mandatory for CPU performance at 200–500 RPS; do NOT use plain `pydantic` or V1), `python-dotenv`
- [ ] Create `api/main.py` — FastAPI app with lifespan context manager for Redis pool and asyncpg pool
- [ ] Create `api/config.py` — load all env vars via `pydantic-settings` `BaseSettings`
- [ ] Create `api/dependencies.py` — `get_redis()` and `get_db()` FastAPI dependency injectors

### 2.2 API Security & Traffic Governance (`[CP]`)
- [ ] Create `api/auth.py` — `verify_api_key()` dependency that checks `x-api-key` header against `X_API_KEY_SECRET`
- [ ] Create `api/rate_limiter.py` — token bucket logic using Redis:
  - [ ] `check_rate_limit(client_key, redis)` → reads `ratelimit:token:{client_key}`, enforces max tokens per window
  - [ ] If bucket exhausted, raise `HTTP 429`
- [ ] Create `api/budget_guard.py`:
  - [ ] `check_budget(client_key, redis)` → reads `budget:daily:{client_key}`, raises `HTTP 402` if exceeded
  - [ ] `deduct_budget(client_key, cost_cents, redis)` → called as a **Celery task** after response, NOT in sync loop

### 2.3 Provider Abstraction Layer (`[DP]`)
- [ ] Add `tenacity` to `requirements.txt`
- [ ] Create `api/providers/base.py` — define `ProviderResponse` dataclass: `[latency_ms, token_count, cost_cents, error_flag, output_text]`
- [ ] Create `api/providers/retry.py` — shared `tenacity` retry decorator for all provider calls:
  - [ ] Configure `@retry(stop=stop_after_attempt(2), wait=wait_fixed(0.1), retry=retry_if_exception_type((httpx.ConnectError, httpx.RemoteProtocolError)))` — retries **at most once** (2 total attempts) on transient network errors only (502, connection drops)
  - [ ] **Hard constraint:** the total time budget across all attempts must remain under the 1500ms SLA. With `wait_fixed(0.1)` (100ms gap) + 2 attempts × ~600ms each, the retry budget is safe
  - [ ] Do **not** retry on `httpx.TimeoutException` — a timeout means the SLA is already breached; fail immediately
  - [ ] Do **not** retry on 4xx client errors (bad prompt, auth failure) — these are deterministic
- [ ] Create `api/providers/groq.py` — `async def call_groq(prompt, model, timeout) -> ProviderResponse`:
  - [ ] Decorate the inner HTTP call function with the retry decorator from `retry.py`
  - [ ] Use `httpx.AsyncClient` with hard `timeout=httpx.Timeout(connect=1.5, read=1.5)` (TTFB 1500ms SLA from PRD §3.9)
  - [ ] Capture wall-clock latency (start before first attempt, end after last), parse token count, calculate `cost_cents` in USD cents per `tradeoffs-info §3`
  - [ ] If all retry attempts exhausted, **then** return `ProviderResponse(error_flag=True)` — never raise out of the provider layer
- [ ] Create `api/providers/openrouter.py` — same pattern as `groq.py`, same retry decorator applied
- [ ] Create `api/providers/fallback.py` — same pattern, targets the designated fallback model endpoint (external API, never local); apply retry decorator
- [ ] Create `api/providers/__init__.py` — `PROVIDER_MAP: dict[str, Callable]` mapping provider name strings to their async callables

### 2.4 L1 Prompt Cache (`[CP]`)
- [ ] Create `api/cache.py`:
  - [ ] `get_cached_response(prompt_hash, redis)` → returns cached JSON or `None`
  - [ ] `set_cached_response(prompt_hash, response, redis, ttl=86400)` → called as a **background task** post-response, NOT in sync path

### 2.5 Adaptive Routing Engine — MAB Policy (`[CP]`)
- [ ] Create `api/mab/state.py`:
  - [ ] `get_mab_weights(redis) -> dict[str, float]` — reads `mab:weights:{provider}` for all providers from Redis
  - [ ] `get_ema_stats(provider, metric, redis) -> tuple[float, float]` — reads `mu` and `var` from Redis for Z-score calc
- [ ] Create `api/mab/router.py`:
  - [ ] `select_provider(policy: str, redis) -> str` — reads MAB weights, applies Thompson Sampling / epsilon-greedy based on policy
  - [ ] Policy modes: `latency-first` (maximize `-Z_latency`), `cost-first` (maximize `-Z_cost`), `sla-aware` (balanced reward `R`)
  - [ ] **READ ONLY from Redis** — no writes inside this function
- [ ] Create `api/mab/reward.py`:
  - [ ] `compute_z_score(x, mu, var, epsilon=1e-5) -> float` — implements formula from `tradeoffs-info §2.2`
  - [ ] `compute_reward(z_quality, z_latency, z_cost, alpha, beta, gamma) -> float` — implements `R = α·Z_q - β·Z_l - γ·Z_c` from `tradeoffs-info §2.3`
  - [ ] **No Redis writes** — pure computation only

### 2.6 Inference API Endpoint (`[DP]`)
- [ ] Create `api/routers/infer.py` — `POST /v1/infer`:
  ```
  1. verify_api_key()                        ← middleware guard
  2. check_rate_limit(client_key, redis)     ← CP: Redis read, raise 429 if exhausted
  3. check_budget(client_key, redis)         ← CP: Redis read, raise 402 if exceeded
  4. hash_prompt → check L1 cache           ← CP: Redis read
  5.   IF cache hit → return cached response (sub-5ms path) → enqueue background log task
  6.   ELSE → select_provider(policy, redis) ← CP: Redis read (MAB weights)
  7.        → call provider async            ← DP: external HTTPS call (1500ms hard timeout)
  8.        → return ProviderResponse to client
  9. Fire-and-forget Celery tasks (NON-BLOCKING):
          - log_inference_telemetry.delay()
          - update_mab_weights.delay()
          - set_cache.delay()
          - deduct_budget.delay()
          - [if sampled] run_hallucination_check.delay()
  ```
- [ ] Add route to `api/main.py` via `app.include_router(infer_router)`
- [ ] Write `tests/test_infer.py` using `httpx.AsyncClient` and FastAPI `TestClient` to assert happy-path response shape

### 2.7 Circuit Breaker (`[CP]` / `[DP]`)
- [ ] Create `api/circuit_breaker.py`:
  - [ ] `is_open(provider, redis) -> bool` — reads `circuit:{provider}:state` from Redis
  - [ ] `record_failure(provider, redis)` — increments `circuit:{provider}:failures`; transitions to `OPEN` if threshold exceeded (fire as a **background task**)
  - [ ] `record_success(provider, redis)` — resets failure count (background task)
  - [ ] On `OPEN` state: `select_provider` skips that provider and routes to fallback

---

## PHASE 3 — Reliability Layer (Celery — Async Workers Only)
> **Strict Rule:** Every task in this phase is a Celery task. None of these functions may be called inline in the sync FastAPI request loop.

### 3.1 Celery App Bootstrap (`[RL]`)
- [ ] Install: add `celery`, `redis` to `requirements.txt`
- [ ] Create `workers/celery_app.py` — initialize Celery using the **dedicated Celery Redis DB**: `broker=REDIS_URL_CELERY, backend=REDIS_URL_CELERY` (**never** use `REDIS_URL_MAB` here — Celery queue backpressure would evict MAB weights under `allkeys-lru`)
- [ ] Configure `task_serializer = 'json'`, `timezone = 'UTC'`, `beat_schedule` for periodic tasks
- [ ] Verify: `docker compose exec worker celery -A workers.celery_app inspect ping`

### 3.2 Telemetry & MAB Update Tasks (`[RL]` + `[DB]`)
- [ ] Create `workers/tasks/telemetry.py`:
  - [ ] `@app.task log_inference_telemetry(payload: dict)`:
    - [ ] Insert one row into `inference_log` via asyncpg
    - [ ] **This is the ONLY place PostgreSQL writes happen for inference data**
  - [ ] `@app.task update_mab_weights(provider: str, latency_ms: int, cost_cents: float, quality_score: float)`:
    - [ ] Read current EMA `mu` and `var` from Redis
    - [ ] Apply EMA update formula: `μ_t = (1-λ)μ_{t-1} + λX_t`, `σ²_t = (1-λ)σ²_{t-1} + λ(X_t - μ_t)²` (`λ=0.1` per `tradeoffs-info §2.1`)
    - [ ] Write updated stats back to Redis (`mab:stats:{provider}:{metric}:mu` and `:var`)
    - [ ] Compute new reward `R` and update `mab:weights:{provider}` in Redis
    - [ ] Also persist updated stats to `provider_stats` table in Postgres (durable backup)

### 3.3 Hallucination / Safety Proxy Task (`[RL]`)
- [ ] Create `workers/tasks/safety.py`:
  - [ ] `@app.task run_hallucination_check(prompt: str, output_text: str, request_id: str)`:
    - [ ] Call a free-tier external API (e.g., OpenRouter Llama-3 endpoint) with a structured safety prompt
    - [ ] Parse binary score from response (`safe=True/False`)
    - [ ] Write result to `inference_log` row (`UPDATE ... WHERE request_id = ...`)
    - [ ] Never use a local model — this is an external API call only (per `tradeoffs-info §5`)
  - [ ] Define sampling rate: only fire the task for ~10% of requests (configurable via env `SAFETY_SAMPLE_RATE`)

### 3.4 Semantic Drift Detection — Nightly Batch (`[RL]`)
- [ ] Install: add `evidently` to `requirements.txt`
- [ ] Create `workers/tasks/drift.py`:
  - [ ] `@app.task run_drift_analysis(run_date: str)`:
    - [ ] Query last 24h of `inference_log` rows from Postgres for each provider
    - [ ] Load "Golden Set" reference distribution (stored as a JSON file in `workers/golden_set.json`)
    - [ ] Run `evidently` `TextDescriptorsDriftMetric` / `DataDriftPreset` on `output_text` column
    - [ ] Write `drift_score` result to `drift_snapshots` table
- [ ] Register as a Celery Beat periodic task: `crontab(hour=2, minute=0)` (2 AM UTC nightly)

### 3.5 Budget Deduction Task (`[RL]`)
- [ ] Create `workers/tasks/budget.py`:
  - [ ] `@app.task deduct_budget(client_key: str, cost_cents: float)`:
    - [ ] Atomically increment `budget:daily:{client_key}` in Redis (via `INCRBYFLOAT`)
    - [ ] If total exceeds limit, also set a `budget:blocked:{client_key}` key (TTL until midnight)

### 3.6 Cache Population Task (`[RL]`)
- [ ] Create `workers/tasks/cache.py`:
  - [ ] `@app.task populate_cache(prompt_hash: str, response_json: str)`:
    - [ ] Write to Redis `cache:prompt:{prompt_hash}` with TTL 86400s
    - [ ] This is fire-and-forget; cache misses on next request are acceptable

---

## PHASE 4 — Inference Economics & Chaos Engine
> Simulated environment features — no real money is spent.

### 4.1 Dynamic Price Feed (`[CP]`)
- [ ] Create `workers/tasks/price_feed.py`:
  - [ ] `@app.task simulate_price_update()`:
    - [ ] Randomly perturb cost multipliers for each provider (e.g., `random.uniform(0.8, 2.5)`)
    - [ ] Write updated cost multipliers to Redis: `pricing:multiplier:{provider}` (TTL 3600s = peak-hour window)
    - [ ] Providers publish new rates; the MAB reward function picks them up on the next request
  - [ ] Register as Celery Beat task: every 15 minutes
- [ ] Update `api/providers/*.py` to read `pricing:multiplier:{provider}` from Redis when calculating `cost_cents`

### 4.2 Failure & Chaos Engine (`[DP]` + `[RL]`)
- [x] Create `api/chaos.py`:
  - [x] `inject_chaos(provider: str) -> ChaosEffect` — reads a chaos config flag from Redis `chaos:{provider}:mode` (`none` | `slow` | `timeout` | `rate_limit`)
  - [x] `slow` mode: adds `asyncio.sleep(random.uniform(1.0, 3.0))` before returning (gray failure simulation)
  - [x] `timeout` mode: immediately raises `httpx.TimeoutException`
  - [x] `rate_limit` mode: returns a synthetic `429` `ProviderResponse` with `error_flag=True`
- [x] Create `api/routers/admin.py` — `POST /admin/chaos` — sets `chaos:{provider}:mode` in Redis (auth-gated endpoint)
- [x] Verify circuit breaker trips correctly when chaos mode forces consecutive failures

### 4.3 Progressive Load Shedding
- [x] Create `api/load_shedder.py`:
  - [x] `should_shed(redis) -> bool` — checks a Redis key `system:load:shed_flag`
  - [x] If `True`, return `HTTP 503` immediately (no LLM call made)
- [x] Create admin endpoint `POST /admin/load-shedding` to toggle the flag
- [x] Add load shedder check as the **first** gate in `POST /v1/infer` before any other logic

---

## PHASE 5 — Observability Pipeline (Non-Blocking)
> **Strict Rule (tradeoffs-info §1):** Langfuse/Arize trace calls are ALWAYS fire-and-forget Celery tasks, never inline.

### 5.1 Langfuse Integration (`[OB]`)
- [x] Install: add `langfuse` to `requirements.txt`
- [x] Create `workers/tasks/observability.py`:
  - [x] `@app.task send_langfuse_trace(request_id, prompt, output, provider, latency_ms, cost_cents)`:
    - [x] Initialize `Langfuse(public_key=..., secret_key=...)` inside the task
    - [x] Create a trace and generation span with all metadata
    - [x] Flush and close — do not hold a persistent connection between tasks

### 5.3 Plug Observability Tasks into Infer Route
- [x] In `api/routers/infer.py`, add to the fire-and-forget block:
  - [x] `send_langfuse_trace.delay(...)` — after non-error response

---

## PHASE 6 — Streamlit Dashboard & Historical Sim (`[DA]`)

### 6.1 Dashboard App Skeleton
- [ ] Install: add `streamlit`, `plotly`, `numpy`, `pandas`, `psycopg2-binary` to `requirements-dashboard.txt` (**do NOT add `asyncpg`** to dashboard deps — see note below)
- [ ] Create `dashboard/app.py` — `st.set_page_config(layout="wide")`
- [ ] Create `dashboard/db.py` — **synchronous** connection helper using `psycopg2-binary` + `pandas.read_sql()`:
  > **⚠️ Streamlit Sync Trap:** Streamlit is synchronous. Wrapping `asyncpg` in `asyncio.run()` creates a new event loop on every page reload. Under a 10k-row Pareto query this freezes the UI and exhausts the Neon connection pool. Use `psycopg2-binary` exclusively here. `asyncpg` stays in the FastAPI backend only.

### 6.2 Live Metrics View
- [ ] Add Streamlit page section **Live Metrics**:
  - [ ] Query last N rows from `inference_log` (configurable slider)
  - [ ] Display `st.metric` cards for: current RPS, p50 latency, p95 latency, total cost in cents (display in INR at UI layer only, per `tradeoffs-info §3`)
  - [ ] Use `st.line_chart` for latency and cost over time

### 6.3 Pareto Frontier Dashboard
- [ ] Implement Pareto dominance filter in `dashboard/pareto.py`:
  - [ ] `compute_pareto_front(df: pd.DataFrame) -> pd.DataFrame`:
    - [ ] Accept columns: `cost_cents`, `latency_ms`, `reliability_index` (1 - error_rate)
    - [ ] Apply strict Pareto dominance: config `A` dominates `B` if `A ≤ B` on all objectives and `A < B` on at least one (per `tradeoffs-info §4`)
    - [ ] Use NumPy vectorized comparison — no Python loops
    - [ ] Return only non-dominated rows
- [ ] In `dashboard/app.py`:
  - [ ] Call `compute_pareto_front()` on historical data
  - [ ] Plot with Plotly Scatter3D: axes = Cost, Latency, Reliability; color-coded by Policy
  - [ ] Render Pareto curve annotation layer

### 6.4 Historical Simulation Engine (Policy Replay)
- [ ] Create `dashboard/policy_replay.py`:
  - [ ] `replay_policy(logs: pd.DataFrame, policy: str, alpha, beta, gamma) -> pd.DataFrame`:
    - [ ] For each row in `inference_log`, re-run the reward function `R = α·Z_q - β·Z_l - γ·Z_c` with alternative weights
    - [ ] Track which provider *would have* been selected — no live API calls are made
    - [ ] Compute cumulative regret vs. the actual policy used: `Regret_T = Σ(R_optimal - R_actual)`
- [ ] Add Streamlit page section **Policy Replay**:
  - [ ] Date range picker for log window
  - [ ] Policy selector: `latency-first`, `cost-first`, `sla-aware`
  - [ ] `st.plotly_chart` for regret curve over time
  - [ ] Summary table: net cost delta, latency delta vs. actual historical path

### 6.5 MAB Weight Inspector
- [ ] Add Streamlit page section **MAB State**:
  - [ ] Connect to Redis and display current `mab:weights:{provider}` for all providers as a `st.bar_chart`
  - [ ] Show EMA `mu` and `var` per provider per metric as a styled `st.dataframe`

---

## PHASE 7 — Load Testing & Benchmarking (k6)

### 7.1 k6 Script Setup
- [ ] Install k6 (local, not in Docker): document install command in `tests/README.md`
- [ ] Create `tests/k6/load_test.js`:
  - [ ] Stage ramp: 0 → 50 RPS → 200 RPS → 500 RPS → 50 RPS over 10 minutes
  - [ ] Include `x-api-key` header and a fixed set of rotating prompts
  - [ ] Define k6 thresholds:
    ```js
    thresholds: {
      http_req_duration: ['p(50)<500', 'p(95)<1500'],
      http_req_failed: ['rate<0.01'],
    }
    ```

### 7.2 Validation Objectives (from PRD §5)
- [ ] Run k6 at 200 RPS and record Uvicorn worker saturation point
- [ ] Capture p95 and p99 latency from k6 HTML report
- [ ] During peak load run, trigger chaos mode via admin API and measure Failure Recovery Time
- [ ] Verify Redis `INFO stats` shows connection pool does not exhaust under MAB sync load
- [ ] Document all benchmark results in `Findings.md`

### 7.3 Regression Benchmark
- [ ] Write `tests/test_p50_regression.py`:
  - [ ] Assert that p50 latency of the `/v1/infer` endpoint stays under 500ms when calling a mocked provider (no network)
  - [ ] Run this in CI to catch sync-path regressions introduced by future changes

---

## PHASE 8 — Security Hardening & Production Readiness

### 8.1 Security
- [ ] Add `slowapi` or custom Redis-backed rate limiter to the FastAPI middleware stack
- [ ] Ensure `X_API_KEY_SECRET` is never logged or returned in any response body
- [ ] Add request ID (`UUID`) header injection in middleware for all responses
- [ ] Verify no provider API keys are ever included in `inference_log` Postgres rows or Langfuse traces

### 8.2 Configuration for Production
- [ ] Update `.env.example` with Upstash Redis URL and Neon Postgres URL examples
- [ ] Add `ENVIRONMENT` env var (`dev` | `prod`)
- [ ] When `ENVIRONMENT=prod`: disable chaos endpoints, enforce stricter rate limits
- [ ] Document Upstash and Neon free-tier limits in `Findings.md`

### 8.3 Final Documentation
- [ ] Update `README.md` with: architecture diagram (text-based), all env vars, docker compose commands, k6 commands
- [ ] Ensure `Findings.md` is up to date with all benchmark results and tradeoff decisions
- [ ] Cross-check every implementation decision against `tradeoffs-info.md` for compliance

---

## CROSS-CUTTING INVARIANTS (Check at Every Phase)
These rules apply at every phase. If any checkbox is ever violated, stop and refactor.

- [ ] **No Postgres write in the sync request loop** — all DB writes go through Celery tasks
- [ ] **No local ML model invoked anywhere** — safety/hallucination scoring uses external API calls only
- [ ] **All cost math in USD cents** — only convert to display currency (INR) in Streamlit
- [ ] **All LLM calls use `httpx` with hard 1500ms TTFB timeout** — no bare `requests` library
- [ ] **MAB weight reads are always from Redis** — never from Postgres in the hot path
- [ ] **Langfuse and Arize calls are always Celery tasks** — never `await`-ed inline
- [ ] **All Redis keys use the namespaced constants from `api/redis_keys.py`** — no magic strings in business logic
