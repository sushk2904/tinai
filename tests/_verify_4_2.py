"""
Phase 4.2 Verification: Failure & Chaos Engine

Orchestrates sequential simulated failure injections to assert API robustness:
  1. Trigger "timeout" mode  -> API safely trips and falls over
  2. Trigger "rate_limit" -> Synthetic 429
  3. Trigger "slow"       -> Gray failure injected into latency
  4. Recover to "none"

Verifies `api/chaos.py` & `api/routers/admin.py`.
"""

import json
import time
import urllib.request
import urllib.error
import subprocess

URL_INFER = "http://localhost:8000/v1/infer"
URL_ADMIN = "http://localhost:8000/admin/chaos"
API_KEY = "5Yp3dNkVwY47Qq8BCsmv1KlNep7VguL0Qs375l4a1QE"

PROVIDER = "fallback" # Using fallback as our test dummy so groq weights aren't trashed

def set_chaos(mode: str):
    print(f"\n[!] Configuring Chaos mode -> {mode} for {PROVIDER}...")
    req = urllib.request.Request(
        URL_ADMIN,
        method="POST",
        headers={"Content-Type": "application/json", "x-api-key": API_KEY},
        data=json.dumps({"provider": PROVIDER, "mode": mode}).encode("utf-8")
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())

def invoke_inference(tag: str):
    t0 = time.time()
    req = urllib.request.Request(
        URL_INFER,
        method="POST",
        headers={"Content-Type": "application/json", "x-api-key": API_KEY},
        data=json.dumps({
            "prompt": f"Write a haiku about testing chaos engineering: {t0}",
            "provider": PROVIDER, # Force the provider
        }).encode("utf-8")
    )
    print(f"    --> Requesting inference [{tag}] ...")
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            print(f"    <-- HTTP OK. Provider: {data.get('provider')}, Latency: {data.get('latency_ms')}ms")
            return data, None
    except urllib.error.HTTPError as e:
        status = e.code
        body = e.read().decode()
        print(f"    <-- HTTP ERROR {status}: {body}")
        return None, (status, body)
    except Exception as e:
        print(f"    <-- EXCEPTION: {e}")
        return None, (500, str(e))

print("="*60)
print("  Phase 4.2 — Chaos Engine Verification")
print("="*60)

# Clear circuit breakers to start fresh
subprocess.run(['docker', 'exec', 'tinai-redis-1', 'redis-cli', '-n', '0', 'DEL', f'circuit:{PROVIDER}:failures', f'circuit:{PROVIDER}:state'])

# 1. Baseline
set_chaos("none")
invoke_inference("Baseline")

# 2. Slow Mode
set_chaos("slow")
invoke_inference("Slow Mode Injection")

# 3. Timeout Mode (Will likely hit 503 circuit trip immediately or synthetic max_timeout)
set_chaos("timeout")
invoke_inference("Timeout Injection")

# 4. Rate Limit Synthesization 
set_chaos("rate_limit")
invoke_inference("Synthetic 429 Injection")

# 5. Clean Up
set_chaos("none")
invoke_inference("Recovery Verification")

print("\n" + "="*60)
print("  Test complete. Verify output metrics above align.")
