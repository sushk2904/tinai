Project: TINAI (This Is Not Artificial Intelligence)
Type: Adaptive AI Execution & Reliability Platform
Core Objective: Dynamically optimize model execution across external providers while continuously evaluating performance, cost, reliability, and behavioral stability under heavy load.

1. Vision & Positioning
TINAI is a control-plane/data-plane AI infrastructure system. It is an AI execution operating layer, not an AI application. It solves the problem of modern AI systems failing silently under load, ignoring cost-performance tradeoffs, and lacking inference-level observability.


2. System Architecture & Layer SeparationThe architecture is strictly separated into asynchronous and synchronous paths to protect the $p50$ latency.Control Plane (Redis/FastAPI): Handles routing intelligence, Multi-Armed Bandit (MAB) policy updates, SLA enforcement, and cost governance. Operates in sub-millisecond latency.
Data Plane (FastAPI/External APIs): Executes high-throughput requests to designated providers.
Reliability Layer (Celery Workers): Asynchronous background tasks handling stability testing, semantic drift detection (via Evidently AI), and hallucination proxy metrics.
Observability Pipeline (Langfuse/Arize): Non-blocking trace capture via background tasks


3. Core Modules
3.1 Provider Abstraction Layer
A unified interface standardizing requests and responses across all external targets (Groq, OpenRouter) and the designated "Internal Fallback" API.
Standardized Payload Output: [Latency (ms), Token Count, Cost Estimate (USD Cents), Error Flag (Boolean), Output Text].

3.2 Adaptive Routing Engine
The brain of the data plane, supporting dynamic policies.
Policies: Latency-first, Cost-first, SLA-aware.
Mechanism: Multi-armed bandit adaptive optimization.
State Management: Weights are cached and read from Redis to prevent worker lock contention.

3.3 Inference Economics & Simulation Engine
Dynamic Price Feeds: A Python-simulated cron job that alters "spot market" pricing for providers (e.g., simulating peak hour surges) to force the Adaptive Routing Engine to dynamically re-route traffic

3.4 Reliability & Behavioral Analysis Module (Asynchronous)
Evaluates payload health without blocking the user response.
Adversarial Proxy: Background workers send request/response pairs to a secondary free-tier API to generate a binary hallucination/safety score.
Semantic Drift: Nightly batch processing using Evidently AI against the PostgreSQL logs to track output entropy and drift from a "Golden Set".

3.5 Failure & Chaos Engine
Built for degradation testing, not just clean failures.
Simulations: Slow token streams (Gray Failures), API timeouts, and simulated rate limits.
Mechanisms: Implements progressive load shedding, circuit breakers, and auto-failover to the designated fallback API.

3.6 Observability & Pareto Dashboard
Metrics Tracked: RPS, $p50$/$p95$ latency, cost per 1K requests, and Reliability Index evolution.
Pareto Frontier: A Streamlit dashboard utilizing NumPy and Plotly to automatically plot optimal configurations (e.g., Cost vs. Latency) based on historical inference logs.

3.7 Historical Simulation Engine (Policy Replay)
Offline Evaluation: Replays stored PostgreSQL inference logs to test alternative routing strategies offline, plotting regret curves for "what-if" scenarios (e.g., "What if we used cost-first routing last month?") without firing live API calls.

3.8 API Security & Traffic Governance
Authentication: Requires an x-api-key header for all Data Plane requests.
Rate Limiting: Enforces client-side token bucket rate limiting via Redis to prevent individual users from saturating Uvicorn worker pools.
Cost Ceilings: Tracks total spend per client key to auto-block requests if a user exceeds their daily simulated budget.

3.9 Tiered Caching Strategy
L1 Cache (Exact Match): Hashes incoming prompts. If an identical prompt exists in Redis (TTL: 24 hours), the system bypasses the MAB router and LLM provider, returning the cached response in <5ms.
Strict Timeout SLAs: Hard limits enforced using httpx timeout configurations. If Time-To-First-Byte (TTFB) exceeds 1500ms, the request is aborted and rerouted to the internal fallback service.


4. Technical Stack & Deployment Model
Designed for a $0 cost, high-throughput serverless architecture.
API Framework: FastAPI running on Uvicorn (Python 3.11+).
Fast Data Plane (State): Redis (Local for dev, Upstash Serverless for Prod).
Persistent Storage: PostgreSQL (Local for dev, Neon Serverless for Prod).
Asynchronous Workers: Celery + Redis Broker.
Frontend Dashboard: Streamlit.
Orchestration: Docker & Docker Compose (docker-compose.yml defining the isolated network).


5. Load & Scale Validation (1M+ Daily Volume)
To prove the architecture's viability, the system must be rigorously benchmarked against production-level traffic.
Target Metric: 1,000,000+ requests per day (~12 Requests Per Second average, requiring burst spike handling of 200-500 RPS).
Testing Tool: Load simulation orchestrated via k6 
Validation Objectives:
Identify Uvicorn worker saturation points.
Document the $p95$ and $p99$ latency distribution under maximum load.
Validate that Redis connection pools do not exhaust during multi-armed bandit state synchronizations.
Measure exact Failure Recovery Time when a provider's circuit breaker trips under sustained spike conditions.


6. Success Criteria
TINAI will be considered ready for production load when it successfully:
Dynamically reduces latency under simulated traffic spikes.
Auto-recovers from a simulated provider failure via circuit breaking.
Calculates and updates MAB weights asynchronously without degrading $p50$ response times.
Produces measurable, documented metrics mapped on a Pareto frontier.
