"""
Phase 2.5 — MAB Policy Routing Verification
8 requests per policy = 24 total live LLM calls (~90 seconds max).
Each prompt is unique (policy + number suffix) so L1 cache never fires.
"""
import urllib.request
import json
import time

URL     = "http://localhost:8000/v1/infer"
API_KEY = "5Yp3dNkVwY47Qq8BCsmv1KlNep7VguL0Qs375l4a1QE"
N       = 8
TIMEOUT = 15   # seconds per request

POLICIES = ["latency-first", "cost-first", "sla-aware"]

def call_api(prompt: str, policy: str) -> dict:
    data = json.dumps({"prompt": prompt, "policy": policy}).encode()
    req  = urllib.request.Request(
        URL, data=data,
        headers={"Content-Type": "application/json", "x-api-key": API_KEY},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode())

all_results = {}
grand_total = N * len(POLICIES)
done = 0

for policy in POLICIES:
    counts   = {}
    failures = 0
    print(f"\n{'='*50}")
    print(f"  Policy: {policy}  ({N} requests)")
    print(f"{'='*50}")
    for i in range(1, N + 1):
        done += 1
        # Unique prompt: policy name embeds in text so different hash per policy
        prompt = f"One word answer: fun fact tag {policy}-{i}?"
        try:
            t0     = time.time()
            result = call_api(prompt, policy)
            ms     = int((time.time() - t0) * 1000)
            prov   = result.get("provider", "unknown")
            cached = result.get("cache_hit", False)
            counts[prov] = counts.get(prov, 0) + 1
            tag = "[CACHE]" if cached else ""
            print(f"  [{done:2d}/{grand_total}] provider={prov:<12} {ms:>5}ms  {tag}")
        except Exception as e:
            failures += 1
            print(f"  [{done:2d}/{grand_total}] ERROR: {str(e)[:60]}")
        time.sleep(0.3)   # 300ms between requests

    all_results[policy] = (counts, failures)

# Summary
print(f"\n{'='*50}")
print("  SUMMARY")
print(f"{'='*50}")
for policy, (counts, failures) in all_results.items():
    total = sum(counts.values())
    row   = f"  {policy:<16}"
    for prov, n in sorted(counts.items(), key=lambda x: -x[1]):
        pct = int(n / total * 100) if total else 0
        row += f"  {prov}: {n}/{total} ({pct}%)"
    if failures:
        row += f"  [ERRORS: {failures}]"
    print(row)

print()
print("Expected:")
print("  latency-first  -> GROQ dominates       (multiplier: groq 3.0x, openrouter 0.3x)")
print("  cost-first     -> OPENROUTER dominates  (multiplier: groq 0.3x, openrouter 3.0x)")
print("  sla-aware      -> BALANCED              (multiplier: both 1.0x)")
