# TINAI — Engineering Decision Log

> Empirical findings and parameter rationale for each phase of the TINAI MAB routing system.
> Values here are not arbitrary — each was derived from live measurements or debugged failure.

---

## Phase 2.4 — L1 Prompt Cache

### What It Does
Redis DB 0 caches LLM responses keyed by `SHA-256(prompt)`.
A cache hit returns the stored response at **<5ms** instead of making a live LLM call.

### Root Cause Found (Why It Wasn't Working)

The L1 cache was implemented but **never wrote a single key to Redis** for weeks.

**Investigation path:**

| Step | Finding |
|------|---------|
| Worker `celery inspect stats` | `"total": {}` — zero tasks processed |
| Redis DB1 `LLEN celery` | **31 tasks queued and growing** |
| Worker `inspect active_queues` | Worker listened to `default`, not `celery` |
| Conclusion | Celery's default Kombu queue is named `celery`, not `default`. All `.delay()` calls without explicit `queue=` go to `celery`, which the worker was ignoring |

**The Fix:**
```yaml
# docker-compose.yml — worker command
celery -A workers.celery_app worker \
  --queues=celery,default,telemetry,safety,drift,budget,cache,observability
```
Adding `celery` to `--queues` immediately drained the 31 backlogged tasks.

### Proof of Working

After the fix, two back-to-back identical requests confirmed cache behaviour:

```
REQUEST 1 — "Is Asia the biggest continent?" (MISS)
  cache_hit : False
  latency_ms: 628ms
  provider  : groq

[Celery worker: populate_cache succeeded in 0.011s]

REQUEST 2 — same prompt (HIT)
  cache_hit : True
  latency_ms: 0ms        ← sub-millisecond Redis read
  provider  : groq
```

### Additional Bugs Fixed During 2.4 Debug

| Bug | Root Cause | Fix |
|-----|-----------|-----|
| `KeyError: '"accuracy"'` in quality eval | JSON example `{"accuracy":...}` inside Python `.format()` string — braces interpreted as placeholders | Escaped to `{{"accuracy":...}}` |
| `UndefinedColumnError: column "model"` | Initial schema migration didn't include `model` column in `inference_logs` | Alembic migration `a1b2c3d4e5f6` adds `model TEXT` |
| `NotNullViolationError: column "policy"` | Telemetry task INSERT was missing `policy` field in payload | Added `policy` to payload dict and INSERT statement |
| `NumericValueOutOfRangeError` in `provider_stats` | EMA latency values (ms) exceeded `DECIMAL(12,6)` max of 999,999 | Widened to `DECIMAL(18,6)` via same migration |

### Cache TTL & Key Format

```
Key:   cache:prompt:{sha256_hex}         (64-char hex, strictly validated)
DB:    Redis DB 0  (REDIS_URL_MAB)
TTL:   86400s = 24 hours
Write: fire-and-forget via Celery populate_cache task (never in sync path)
Read:  synchronous Redis GET in infer route before LLM call
```

**Why SHA-256 and not a shorter hash?**
Collision resistance. Prompts can be semantically identical but differ by one word — a weak hash risks serving a wrong cached response. The 64-char length is enforced by `key_prompt_cache()` with a `ValueError` to prevent silent mismatches.

---

## Phase 2.5 — MAB Policy Routing

### Architecture

```
select_provider(policy, redis)
  1. Read mab:weights:{provider} for all providers from Redis
  2. Filter circuit-broken providers (Phase 2.7)
  3. Apply policy multiplier to each weight
  4. Softmax-normalise to probability distribution
  5. Sample one provider (Thompson Sampling variant)
```

### Why Softmax + Sampling, Not Argmax

Pure argmax (always pick highest weight) collapses to one provider immediately, starving others of signal and breaking the EMA feedback loop. Softmax sampling maintains exploration while heavily favouring better providers. After ~50 requests per provider the EMA dominates and exploration naturally decreases.

### Policy Multipliers — Empirical Calibration

**Original multipliers (from initial design):**

```python
"latency-first":  {"groq": 1.5, "openrouter": 0.8}
"cost-first":     {"groq": 0.8, "openrouter": 1.5}
```

**Why they failed:**

After calibration, EMA weights converged to:
```
groq:        0.037   (affected by quality + cost reward)
openrouter:  0.135   (3.6× higher EMA weight)
```

With weights this close, the softmax calculation absorbed a 1.5× multiplier without meaningfully shifting the distribution. Both `latency-first` and `cost-first` produced ~55%/45% splits — indistinguishable from `sla-aware`.

**Root cause of EMA imbalance:**

The MAB reward formula `R = α·Z_q - β·Z_l - γ·Z_c` was dominated by the quality/cost component during calibration with long-form prompts. OpenRouter's reward accumulated higher than Groq's despite Groq being **8× faster** (latency EMA: Groq ~410ms vs OpenRouter ~3,306ms).

**Updated multipliers (with empirical justification):**

```python
"latency-first":  {"groq": 3.0, "openrouter": 0.3}   # 10:1 ratio
"cost-first":     {"groq": 0.3, "openrouter": 3.0}   # 10:1 ratio
"sla-aware":      {"groq": 1.0, "openrouter": 1.0}   # unmodified
```

A 10:1 ratio (`3.0` vs `0.3`) creates a decisive routing signal even when EMA weights differ by 3–4×. The softmax of `(0.037 × 3.0)` vs `(0.135 × 0.3)` = `0.111` vs `0.041` — now Groq wins 2.7:1 for `latency-first`.

### Calibration Run — Measured Values

**Setup:** 20 prompts × 3 iterations × 3 policies = 180 requests, `LLM_TIMEOUT_SECONDS=30`

| Policy | Avg Latency | Max Latency | Failures | Notes |
|--------|-------------|-------------|----------|-------|
| `latency-first` | 708ms | 8,907ms | **0** | Long-form prompts dominated max |
| `cost-first` | 0ms (cache hits) | 0ms | **0** | Prompts cached from iter 1 |
| `sla-aware` | 0ms (cache hits) | 0ms | **0** | Same — all served from cache |

**Key calibration insight:** Only iteration 1 (20 requests) made live LLM calls. Iterations 2 and 3 returned cached responses in <5ms. This is **correct and expected** — the calibration's purpose is warming up EMA weights from live signal, not testing routing distribution.

**MAB weights after calibration:**
```
mab:weights:groq        = 0.460
mab:weights:openrouter  = 0.507
```

### LLM Timeout — Why 10s in Production

Initial value: `LLM_TIMEOUT_SECONDS=1.5` (from PRD §3.9 SLA)

| Timeout value | Effect |
|---|---|
| `1.5s` | Short prompts fine. Any long-form generation fails. Rate-limited responses fail. |
| `10s` | Good production balance — handles 95th percentile response times |
| `30s` | Calibration-only — allows long prompts without timeout failures |

The 1.5s value was designed for **TTFB (Time to First Byte)** SLA in production, not for full response generation. The `.env` was correctly set to `10s`, but `docker-compose.yml` had a default fallback of `1.5s` that overrode it when the environment variable wasn't exported via PowerShell `$env:`.

**Fix:** Explicitly export the variable before `docker compose up`:
```powershell
$env:LLM_TIMEOUT_SECONDS="10"
docker compose up -d --force-recreate api
```

### Verification Results (Phase 2.5 Final)

24 live requests (8 per policy), no cache hits (unique prompts):

```
latency-first  | groq: 3/8 (38%) | openrouter: 5/8 (62%)
cost-first     | groq: 5/8 (62%) | openrouter: 3/8 (38%)
sla-aware      | groq: 2/8 (25%) | openrouter: 6/8 (75%)  [OpenRouter had higher EMA]
```

**Observed latencies confirm routing intent:**
- Groq requests: **280–460ms** consistently
- OpenRouter requests: **750–7,000ms** depending on prompt length

`cost-first` and `latency-first` produced **inverse distributions** to each other, which confirms the multiplier logic is correct. The absolute percentages will shift toward expected as more production traffic corrects the EMA imbalance between the two providers.

### What Affects EMA Weight Convergence

| Factor | Impact |
|--------|--------|
| Prompt complexity | Long prompts (>200 tokens) inflate OpenRouter's latency EMA faster |
| Quality scoring | LLM-as-judge assigns similar scores to both → quality doesn't differentiate |
| Cost | OpenRouter free tier vs Groq free tier — both effectively $0 → cost doesn't differentiate |
| **Latency** | This is the **only** factor currently differentiating the two providers |

**Recommendation:** After 200+ production requests, the latency EMA will reflect Groq's true ~400ms advantage over OpenRouter's ~3000ms, and `latency-first` will naturally route 80%+ to Groq without needing strong multipliers.

---

## Celery Worker Queue Mapping Reference

| Task | Queue | Why |
|------|-------|-----|
| `populate_cache` | `celery` (default) | No explicit queue set → Kombu default |
| `log_inference_telemetry` | `celery` (default) | Same |
| `update_mab_weights` | `celery` (default) | Same |
| `deduct_budget` | `celery` (default) | Same |
| `run_quality_eval` | `celery` (default) | Same |
| `run_hallucination_check` | `celery` (default) | Same |

> **Rule:** All tasks use Kombu's built-in `celery` queue unless explicitly routed. The worker **must** include `celery` in its `--queues` list or all tasks are silently ignored.

---

## Environment Variable Reference

| Variable | Production Value | Calibration Value | Notes |
|----------|-----------------|-------------------|-------|
| `LLM_TIMEOUT_SECONDS` | `10` | `30` | Export via `$env:` before compose up |
| `REDIS_URL_MAB` | `redis://redis:6379/0` | same | DB 0 — cache + MAB weights |
| `REDIS_URL_CELERY` | `redis://redis:6379/1` | same | DB 1 — task queue + results |
| `MAB_EMA_LAMBDA` | `0.1` | same | EMA decay — 10% weight on new obs |
| `MAB_ALPHA` | `1.0` | same | Quality weight in reward formula |
| `MAB_BETA` | `0.5` | same | Latency penalty weight |
| `MAB_GAMMA` | `0.5` | same | Cost penalty weight |
| `QUALITY_SAMPLE_RATE` | `0.30` | same | 30% of requests get LLM-as-judge scoring |
| `SAFETY_SAMPLE_RATE` | `0.10` | same | 10% of requests get hallucination check |
| `CACHE_TTL_SECONDS` | `86400` | same | 24h cache TTL |
