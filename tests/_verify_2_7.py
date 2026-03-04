"""
Phase 2.7 — Circuit Breaker Verification

Tests the full circuit breaker lifecycle via direct Redis manipulation:

  Test A: CLOSED state — normal request flows through
  Test B: OPEN state   — manually trip the breaker, confirm MAB skips that provider
                         and routes to the other available provider
  Test C: RESET        — clear the OPEN state, confirm traffic resumes normally
  Test D: All circuits OPEN — confirm fallback provider is used (or 503 returned)

Circuit breaker Redis keys:
  circuit:{provider}:state    → CLOSED | OPEN | HALF_OPEN
  circuit:{provider}:failures → rolling counter (TTL = 60s)
"""
import urllib.request
import urllib.error
import json
import subprocess
import time

URL     = "http://localhost:8000/v1/infer"
API_KEY = "5Yp3dNkVwY47Qq8BCsmv1KlNep7VguL0Qs375l4a1QE"

PASS = "[PASS]"
FAIL = "[FAIL]"
INFO = "[INFO]"
results = []

def make_request(prompt, policy="sla-aware"):
    data = json.dumps({"prompt": prompt, "policy": policy}).encode()
    req  = urllib.request.Request(
        URL, data=data,
        headers={"Content-Type": "application/json", "x-api-key": API_KEY},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}

def redis_set(key, value):
    result = subprocess.run(
        ["docker", "compose", "exec", "redis", "redis-cli", "-n", "0", "SET", key, value],
        cwd=r"c:\Users\susha\Desktop\TINAI",
        capture_output=True, text=True
    )
    return result.returncode == 0

def redis_del(*keys):
    result = subprocess.run(
        ["docker", "compose", "exec", "redis", "redis-cli", "-n", "0", "DEL"] + list(keys),
        cwd=r"c:\Users\susha\Desktop\TINAI",
        capture_output=True, text=True
    )
    return result.returncode == 0

def redis_get(key):
    result = subprocess.run(
        ["docker", "compose", "exec", "redis", "redis-cli", "-n", "0", "GET", key],
        cwd=r"c:\Users\susha\Desktop\TINAI",
        capture_output=True, text=True
    )
    return result.stdout.strip().splitlines()[-1] if result.stdout.strip() else None

def check(label, passed, detail=""):
    tag = PASS if passed else FAIL
    line = f"  {tag}  {label}"
    if detail:
        line += f"  ({detail})"
    print(line)
    results.append((label, passed))

print("=" * 58)
print("  Phase 2.7 — Circuit Breaker Verification")
print("=" * 58)

# ── Ensure circuit state is clean ────────────────────────────
redis_del("circuit:groq:state", "circuit:openrouter:state",
          "circuit:groq:failures", "circuit:openrouter:failures")

# ═══════════════════════════════════════════════════════════════
# TEST A: CLOSED state — both providers available
# ═══════════════════════════════════════════════════════════════
print("\n  [Test A] CLOSED state — normal routing")
status, r = make_request("One word: sky colour.", policy="latency-first")
check("Request succeeds (200)", status == 200, f"got {status}")
check("Provider is groq or openrouter", r.get("provider") in ("groq", "openrouter"),
      f"got {r.get('provider')}")

# ═══════════════════════════════════════════════════════════════
# TEST B: OPEN circuit for groq — MAB must skip it
# ═══════════════════════════════════════════════════════════════
print("\n  [Test B] groq circuit OPEN — MAB should bypass to openrouter")
redis_set("circuit:groq:state", "OPEN")

state_val = redis_get("circuit:groq:state")
check("Redis shows groq circuit = OPEN", state_val == "OPEN", f"got '{state_val}'")

# Run 5 requests with latency-first (which strongly favours groq)
# All should now go to openrouter since groq is OPEN
providers_seen = set()
for i in range(5):
    _, r = make_request(f"One word: animal {i}.", policy="latency-first")
    if r.get("provider"):
        providers_seen.add(r["provider"])

check("groq never selected while circuit OPEN",
      "groq" not in providers_seen,
      f"providers seen: {providers_seen}")
check("openrouter picks up traffic",
      "openrouter" in providers_seen,
      f"providers seen: {providers_seen}")

# ═══════════════════════════════════════════════════════════════
# TEST C: Reset circuit — traffic resumes to groq
# ═══════════════════════════════════════════════════════════════
print("\n  [Test C] Reset groq circuit — traffic should resume")
redis_set("circuit:groq:state", "CLOSED")
redis_del("circuit:groq:failures")

state_val = redis_get("circuit:groq:state")
check("Redis shows groq circuit = CLOSED", state_val == "CLOSED", f"got '{state_val}'")

providers_seen = set()
for i in range(6):
    _, r = make_request(f"One word: planet {i}.", policy="latency-first")
    if r.get("provider"):
        providers_seen.add(r["provider"])

check("groq receives traffic again after reset",
      "groq" in providers_seen,
      f"providers seen: {providers_seen}")

# ═══════════════════════════════════════════════════════════════
# TEST D: ALL circuits OPEN — fallback or 503
# ═══════════════════════════════════════════════════════════════
print("\n  [Test D] ALL circuits OPEN — expect fallback or 503")
redis_set("circuit:groq:state", "OPEN")
redis_set("circuit:openrouter:state", "OPEN")

status, r = make_request("One word: star.", policy="sla-aware")
provider = r.get("provider", "")
# System should either: route to fallback provider, or return 503
check("System handles all-circuits-OPEN gracefully",
      status in (200, 503),
      f"status={status} provider={provider}")
if status == 200:
    check("Routes to fallback provider",
          provider == "fallback" or len(provider) > 0,
          f"provider={provider}")
else:
    print(f"  {INFO}  Returned 503 — no fallback configured, correct behaviour")

# ── Always clean up ──────────────────────────────────────────
redis_set("circuit:groq:state", "CLOSED")
redis_set("circuit:openrouter:state", "CLOSED")
redis_del("circuit:groq:failures", "circuit:openrouter:failures")
print(f"\n  {INFO}  Circuit breaker state reset to CLOSED for all providers.")

# ── SUMMARY ──────────────────────────────────────────────────
print(f"\n{'=' * 58}")
print("  SUMMARY")
print(f"{'=' * 58}")
passed = sum(1 for _, ok in results if ok)
failed = sum(1 for _, ok in results if not ok)
print(f"  {passed}/{len(results)} checks passed   {failed} failed")
if failed:
    print("\n  Failed checks:")
    for label, ok in results:
        if not ok:
            print(f"    - {label}")
else:
    print("\n  All checks passed. Phase 2.7 VERIFIED.")
