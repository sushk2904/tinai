"""
TINAI MAB Calibration Script
Sends 20 prompts x 5 iterations (100 requests total) across all 3 policies
to warm up EMA weights and give the MAB enough signal for routing decisions.

Endpoint: POST /v1/infer
Payload:  { "prompt": str, "policy": str }
Response: { "output_text", "provider", "model", "latency_ms", "cache_hit", ... }
"""

import urllib.request
import json
import time
import statistics

# --- CONFIGURATION ---
API_URL = "http://localhost:8000/v1/infer"
API_KEY = "5Yp3dNkVwY47Qq8BCsmv1KlNep7VguL0Qs375l4a1QE"
ITERATIONS = 3   # 20 prompts x 3 iterations = 60 requests per policy
REQUEST_TIMEOUT = 60        # seconds — some prompts are long-form generation
INTER_REQUEST_SLEEP = 0.5   # seconds between requests — avoids 429 rate limiting
INTER_POLICY_SLEEP  = 15    # seconds cool-down between policies

POLICIES = ["latency-first", "cost-first", "sla-aware"]

# 20 precisely engineered prompts from your calibrate_mab.py
PROMPTS = [
    # Category 1: TTFB Testers (short, instant answers)
    "What is the capital of France? Reply with exactly one word.",
    "Convert 100 USD to EUR based on historical averages. One sentence.",
    "Who won the FIFA World Cup in 2022?",
    "Name the 3rd planet from the Sun.",
    "Translate 'Hello, how are you?' into French.",
    # Category 2: Throughput Testers
    "Write a 3-paragraph story about a cybernetic dog.",
    "List 10 distinct features of Python 3.10 and explain each in one sentence.",
    "Write a polite 200-word email declining a job offer.",
    "Explain the history of the Roman Empire in 4 paragraphs.",
    "Generate a recipe for chocolate cake with step-by-step instructions.",
    # Category 3: Quality Testers
    "If I have 3 apples, eat 1, buy 5, and give half to a friend, how many do I have?",
    "Why is a manhole cover round instead of square? Give the engineering reason.",
    "Explain Quantum Entanglement to a 5-year-old.",
    "I am in a race. I pass the person in 2nd place. What place am I in?",
    "Summarize the core difference between TCP and UDP networking protocols.",
    # Category 4: Constraint Testers
    "Output the numbers 1, 2, and 3 in valid JSON format. Provide no other text.",
    "Write a haiku about artificial intelligence. Do not use the letter 'e'.",
    "Return exactly this string: status ok. No other text.",
    "Write a python function to reverse a string. Only output the code block.",
    "Name a country starting with 'Z'. Stop generating immediately after the name.",
]


def call_api(prompt: str, policy: str) -> dict:
    data = json.dumps({"prompt": prompt, "policy": policy}).encode()
    req = urllib.request.Request(
        API_URL, data=data,
        headers={"Content-Type": "application/json", "x-api-key": API_KEY},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def run_calibration():
    total_requests = len(PROMPTS) * ITERATIONS * len(POLICIES)
    print(f"TINAI MAB Calibration")
    print(f"Policies: {', '.join(POLICIES)}")
    print(f"Total Requests: {total_requests}  ({len(PROMPTS)} prompts x {ITERATIONS} iters x {len(POLICIES)} policies)")
    print()

    # Track per-policy stats
    policy_results: dict[str, dict] = {
        p: {"latencies": [], "failures": 0, "providers": {}} for p in POLICIES
    }

    req_num = 0
    for p_idx, policy in enumerate(POLICIES):
        if p_idx > 0:
            print(f"\n  Cooling down {INTER_POLICY_SLEEP}s before next policy (avoiding rate limits)...")
            time.sleep(INTER_POLICY_SLEEP)
        print(f"{'='*70}")
        print(f"  Policy: {policy.upper()}")
        print(f"{'='*70}")
        print(f"  {'#':<4} | {'Prompt':<35} | {'Provider':<12} | {'Latency':>9} | Cache")
        print(f"  {'-'*75}")

        for iteration in range(ITERATIONS):
            for idx, prompt in enumerate(PROMPTS):
                req_num += 1
                snippet = (prompt[:32] + "...") if len(prompt) > 32 else prompt

                try:
                    result = call_api(prompt, policy)
                    provider = result.get("provider", "unknown")
                    latency  = result.get("latency_ms", -1)
                    cache    = result.get("cache_hit", False)

                    policy_results[policy]["latencies"].append(latency)
                    policy_results[policy]["providers"][provider] = (
                        policy_results[policy]["providers"].get(provider, 0) + 1
                    )

                    cache_tag = "[CACHE]" if cache else ""
                    print(f"  {req_num:<4} | {snippet:<35} | {provider:<12} | {latency:>6}ms  | {cache_tag}")

                except Exception as e:
                    policy_results[policy]["failures"] += 1
                    print(f"  {req_num:<4} | {snippet:<35} | {'ERROR':<12} | {'N/A':>9} | {type(e).__name__}: {str(e)[:30]}")

                time.sleep(INTER_REQUEST_SLEEP)  # avoid 429 rate limiting

    # --- FINAL REPORT ---
    print(f"\n{'='*70}")
    print(f"  CALIBRATION COMPLETE - SUMMARY")
    print(f"{'='*70}")

    for policy in POLICIES:
        r = policy_results[policy]
        lats = r["latencies"]
        provs = r["providers"]
        total = sum(provs.values())
        failures = r["failures"]
        print(f"\n  [{policy}]")
        if lats:
            print(f"    Avg latency   : {statistics.mean(lats):.0f}ms")
            print(f"    Median latency: {statistics.median(lats):.0f}ms")
            print(f"    Max latency   : {max(lats):.0f}ms")
        print(f"    Failures      : {failures}")
        print(f"    Distribution  :", end="")
        for prov, n in sorted(provs.items(), key=lambda x: -x[1]):
            pct = int(n / total * 100) if total else 0
            print(f"  {prov}: {n}/{total} ({pct}%)", end="")
        print()

    print()

    # Check current MAB weights after calibration
    print(f"  Run after completion:")
    print(f"    docker compose exec redis redis-cli -n 0 MGET mab:weights:groq mab:weights:openrouter")


if __name__ == "__main__":
    run_calibration()