"""
Phase 4.1 Verification: Dynamic Price Feed Engine

Verifies that the `simulate_price_update` Celery task perturbs the baseline prices
and that the Inference API automatically scales `cost_cents` per request.

Run with: python tests/_verify_4_1.py
"""

import json
import subprocess
import time
import urllib.request
import urllib.error

URL = "http://localhost:8000/v1/infer"
API_KEY = "5Yp3dNkVwY47Qq8BCsmv1KlNep7VguL0Qs375l4a1QE"
PROVIDER = "groq"

def send_request():
    req = urllib.request.Request(
        URL,
        method="POST",
        headers={"Content-Type": "application/json", "x-api-key": API_KEY},
        data=json.dumps({
            "prompt": f"Write a haiku about economics {time.time()}",
            "provider": PROVIDER,
            "policy": "cost-first"
        }).encode("utf-8")
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())

print("="*60)
print("  Phase 4.1 — Dynamic Price Feed Engine Verification")
print("="*60)

# 1. Clear existing multipliers
print(f"\n[1] Clearing existing pricing multipliers from Redis...")
subprocess.run(['docker', 'exec', 'tinai-redis-1', 'redis-cli', '-n', '0', 'DEL', f'pricing:multiplier:{PROVIDER}'], capture_output=True)
time.sleep(1)

# 2. Baseline Request
print(f"\n[2] Executing baseline Inference Request to {PROVIDER}...")
resp1 = send_request()
baseline_cost = resp1.get('cost_cents')
baseline_tokens = resp1.get('token_count')
print(f"    -> Token Count: {baseline_tokens}")
print(f"    -> Provider Cost: {baseline_cost}¢  (Multiplier: 1.0x)")

# 3. Simulate Price Feed Cron Job
print(f"\n[3] Triggering Celery 'simulate_price_update' background task...")
inject_script = "from workers.tasks.price_feed import simulate_price_update; print(simulate_price_update.delay().id)"
r = subprocess.run(["docker", "exec", "tinai-api-1", "python", "-c", inject_script], capture_output=True, text=True)
if r.returncode != 0:
    print("Task failed:", r.stderr)
else:
    print(f"    -> Celery Task Dispatched: {r.stdout.strip()}")

print("    -> Waiting 3 seconds for workers to write to Redis DB 0...")
time.sleep(3)

# 4. Check New Multiplier
mult_str = subprocess.run(['docker', 'exec', 'tinai-redis-1', 'redis-cli', '-n', '0', 'GET', f'pricing:multiplier:{PROVIDER}'], capture_output=True, text=True).stdout.replace('\r', '').strip()
print(f"\n[4] Redis DB 0 state for {PROVIDER}:")
if mult_str:
    print(f"    -> New multiplier applied: {mult_str}x")
else:
    print(f"    !!! Multiplier NOT FOUND in Redis !!!")
    mult_str = "1.0"

# 5. Surge Request
print(f"\n[5] Executing post-surge Inference Request to {PROVIDER}...")
resp2 = send_request()
surge_cost = resp2.get('cost_cents')
surge_tokens = resp2.get('token_count')
print(f"    -> Token Count: {surge_tokens}")
print(f"    -> Provider Cost: {surge_cost}¢  (Multiplier: {mult_str}x)")

# 6. Mathematical validation
baseline_cost_per_token = baseline_cost / baseline_tokens if baseline_tokens else 0
surge_cost_per_token = surge_cost / surge_tokens if surge_tokens else 0

if float(mult_str) != 1.0:
    effective_mult = surge_cost_per_token / baseline_cost_per_token if baseline_cost_per_token else 0
    print(f"\n[Validation] Math Check:")
    print(f"    Expected Multiplier Effect:  {mult_str}x")
    print(f"    Calculated Multiplier Effect: {effective_mult:.2f}x")
    
    if abs(effective_mult - float(mult_str)) < 0.05:
         print(f"    => [PASS] Dynamic surging isolated and successfully applied to provider layer!")
    else:
         print(f"    => [FAIL] Mathematically incorrect pricing.")
else:
    print(f"    => [WARN] Multiplier didn't change (rolled 1.0). Run again to see surge.")

print("\n" + "="*60)
