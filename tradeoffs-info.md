# TINAI: Architectural & Mathematical Tradeoffs (Source of Truth)

**CRITICAL DIRECTIVE FOR AI AGENTS:** This document contains the unbreakable laws of physics for the TINAI architecture. Do not hallucinate alternative mathematical models, do not introduce synchronous database writes, and **do not attempt to load local ML models.** The system must be engineered to survive 1M+ requests/day (bursts of 200-500 RPS).

## 1. The 1M/Day Latency Rule (Strict Async Boundary)
To maintain a high-throughput data plane, the system architecture mandates a strict separation between the execution loop and the observability loop.

* **Allowed in the Synchronous FastAPI Request Loop (The Data Plane):**
  1. Reading Multi-Armed Bandit (MAB) weights from the Redis cache.
  2. Executing the asynchronous HTTP call to the designated external LLM provider.
  3. Returning the response to the client.
* **Strictly Forbidden in the Request Loop (Must be offloaded to Celery/Background Tasks):**
  1. PostgreSQL database writes (Logging inference telemetry).
  2. Hallucination and safety checking.
  3. Semantic drift calculations.
  4. Trace logging to observability platforms (Langfuse/Arize).

## 2. Multi-Armed Bandit (MAB) Mathematical Foundation
To prevent variable domination (e.g., a 1000ms latency spike mathematically drowning out a $0.001 cost difference), all metrics must be standardized using an Exponential Moving Average (EMA) Z-Score *before* entering the reward function.

**2.1 Running Statistics ($O(1)$ updates in Redis per provider)**
For any metric $X$ (Latency, Cost, Quality), update the EMA mean ($\mu$) and variance ($\sigma^2$) using decay factor $\lambda = 0.1$:
$$\mu_{X,t} = (1 - \lambda)\mu_{X,t-1} + \lambda X_t$$
$$\sigma^2_{X,t} = (1 - \lambda)\sigma^2_{X,t-1} + \lambda (X_t - \mu_{X,t})^2$$

**2.2 Real-Time Z-Score Normalization**
Normalize the incoming metric before calculating the reward (where $\epsilon = 10^{-5}$ to prevent division by zero):
$$Z_{X} = \frac{X_t - \mu_{X,t}}{\sqrt{\sigma^2_{X,t}} + \epsilon}$$

**2.3 The Reward Function**
Calculate the final reward ($R$) for the routing decision. The system maximizes $R$:
$$R = \alpha(Z_{quality}) - \beta(Z_{latency}) - \gamma(Z_{cost})$$
*Implementation Note: The weights ($\alpha, \beta, \gamma$) are dynamically shifted by the Control Plane based on the user's selected policy (e.g., a "Cost-First" policy heavily weights $\gamma$).*

## 3. Cost Calculation Standard
* **Currency Constraint:** All internal backend math MUST be calculated in **USD Cents** (e.g., $0.01) to prevent floating-point underflow errors from micro-transactions.
* **UI Display:** Only convert to localized currency (e.g., INR) at the Streamlit presentation layer.

## 4. Pareto Frontier Calculation (Dashboard)
To plot the Pareto-optimal configurations in Streamlit, use a strict dominance algorithm on historical data arrays.
* **Dominance Logic:** A routing configuration $A$ strictly dominates $B$ if $A$ is equal or better in all objectives (Cost, Latency, Reliability) and strictly better in at least one.
* **Execution:** Filter the historical dataset using NumPy to only include non-dominated points, then plot the frontier curve using Plotly.

## 5. The "No Local Compute" Cloud-Native Pivot
TINAI operates as a highly scalable, serverless-compatible microservice. It does not rely on local GPU compute or bare-metal ML models.
* **The "Internal Fallback Model":** This is mathematically treated as an internal node by the router, but is physically implemented as a highly reliable, designated external API endpoint (e.g., a specific fallback model on Groq or Google AI Pro).
* **Agentic Red-Teaming (The Proxy Model):** When a prompt is flagged for sampling, the FastAPI app fires a Celery task. The Celery worker sends the prompt/response to a free-tier external API (e.g., an OpenRouter Llama-3 endpoint) to generate a binary safety score, completely eliminating the need to run local models like Phi-3.5.