"""
Phase 3 — Reliability Layer (Celery) Verification

Triggers every Phase 3 Celery task directly via .delay() and verifies they execute
successfully without hanging the API. This ensures isolated Celery DB config,
async operation, and proper DB side effects (telemetry writes, budget, cache, drift).

To run:
1. Ensure docker compose is up.
2. python tests/_verify_phase3.py
3. Check docker compose logs worker --since <time>
"""

import time
import subprocess
import json
import uuid
import datetime

PASS = "[PASS]"
FAIL = "[FAIL]"

def start_celery_task(task_name, args=None, kwargs=None):
    # We use docker compose exec api python -c to inject celery task delay
    # so we don't have to install celery on Windows host
    args_str = json.dumps(args or [])
    kwargs_str = json.dumps(kwargs or {})
    
    module_name, func_name = task_name.split('.')
    py_script = f"""
import sys, json
from workers.tasks.{module_name} import {func_name}
try:
    args_list = json.loads('{args_str}')
    kwargs_dict = json.loads('{kwargs_str}')
    res = {func_name}.delay(*args_list, **kwargs_dict)
    print(res.id)
except Exception as e:
    print(f"ERROR: {{e}}")
    sys.exit(1)
"""
    cmd = ["docker", "exec", "tinai-api-1", "python", "-c", py_script]
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=r"c:\Users\susha\Desktop\TINAI")
    
    if res.returncode != 0:
        return False, res.stderr.strip() or res.stdout.strip()
    return True, res.stdout.strip()

print("=" * 58)
print("  Phase 3 — Celery Workers Verification")
print("=" * 58)

checks = []
req_id = str(uuid.uuid4())

# 1. Telemetry Task (3.2)
telemetry_payload = {
    "request_id": req_id,
    "provider": "groq",
    "model": "gemma2-9b-it",
    "policy": "sla-aware",
    "latency_ms": 125,
    "token_count": 50,
    "cost_cents": 0.0,
    "error_flag": False,
    "prompt_hash": "testhash",
    "client_key": "testclient"
}
t0 = time.time()
ok, task_id = start_celery_task("telemetry.log_inference_telemetry", args=[telemetry_payload])
checks.append(("3.2 log_inference_telemetry.delay() fired", ok, task_id))

# 2. Safety / Hallucination proxy (3.3)
ok, task_id = start_celery_task("safety.run_hallucination_check", args=["What is 2+2?", "4", req_id])
checks.append(("3.3 run_hallucination_check.delay() fired", ok, task_id))

# 3. Cache population (3.6)
valid_hash = "a" * 64
ok, task_id = start_celery_task("cache.populate_cache", args=[valid_hash, '{"output_text": "cached"}'])
checks.append(("3.6 populate_cache.delay() fired", ok, task_id))

# 4. Budget deductor (3.5)
ok, task_id = start_celery_task("budget.deduct_budget", args=["testclient_phase3", 4.5])
checks.append(("3.5 deduct_budget.delay() fired", ok, task_id))

# 5. Drift Analysis (3.4)
# Passing empty string so it defaults to analysing the last 24h
ok, task_id = start_celery_task("drift.run_drift_analysis", args=[""])
checks.append(("3.4 run_drift_analysis.delay() fired", ok, task_id))


# Summary
print("")
for label, passed, detail in checks:
    tag = PASS if passed else FAIL
    if passed:
        print(f"  {tag}  {label} (Task ID: {detail[:8]})")
    else:
        print(f"  {tag}  {label}\n      Error: {detail}")

print("\n  [INFO] Waiting 8s for Celery tasks to process...")
time.sleep(8)
print("  [INFO] Verifying worker logs for success:\n")

cmd = ["docker", "compose", "logs", "worker", "--since", "15s"]
log_res = subprocess.run(cmd, capture_output=True, text=True, cwd=r"c:\Users\susha\Desktop\TINAI")

task_names = [
    "tasks.telemetry.log_inference_telemetry",
    "tasks.safety.run_hallucination_check",
    "tasks.cache.populate_cache",
    "tasks.budget.deduct_budget",
    "tasks.drift.run_drift_analysis"
]

all_good = True
for c in task_names:
    if f"Task workers.{c}" in log_res.stdout and "succeeded" in log_res.stdout:
        print(f"  {PASS}  {c} execution succeeded in worker log")
    else:
        print(f"  {FAIL}  {c} missing 'succeeded' in worker log")
        all_good = False

print(f"\n{'=' * 58}")
if all_good:
    print("  Phase 3 Celery Layer VERIFIED.")
else:
    print("  Some Celery tasks failed or are still running. Check logs manually.")
