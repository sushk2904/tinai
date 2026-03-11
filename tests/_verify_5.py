"""
Phase 5.1 & 5.3 Verification: Langfuse Observability Pipeline

Fires a test inference request, forcing the async worker to push the telemetry 
out to the Langfuse engine in a non-blocking background task.
"""

import json
import time
import urllib.request
import urllib.error

API_KEY = "5Yp3dNkVwY47Qq8BCsmv1KlNep7VguL0Qs375l4a1QE"
URL = "http://localhost:8000/v1/infer"

print("="*60)
print("  Phase 5 — Observability Pipeline Verification")
print("="*60)
print("\n[!] Dispatched test inference request...")

req = urllib.request.Request(
    URL,
    method="POST",
    headers={"Content-Type": "application/json", "x-api-key": API_KEY},
    data=json.dumps({"prompt": f"Write a haiku about telemetry: {time.time()}", "provider": "fallback"}).encode("utf-8")
)
try:
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode())
        req_id = data.get("request_id")
        print(f"    <-- HTTP OK. Latency: {data.get('latency_ms')}ms")
        print(f"    <-- Request ID: {req_id}")
        print("\n[!] The API response completed. Worker should now be asynchronously pushing to Langfuse.")
        print(f"    (Verify `docker logs tinai-worker-1` for success message matching {req_id})")
        
        # Checking container logs directly for validation
        import subprocess
        print("\n[!] Polling Celery Background Worker Logs (~3 seconds)...")
        time.sleep(3)
        res = subprocess.run(["docker", "logs", "--tail", "50", "tinai-worker-1"], capture_output=True, text=True)
        logs = res.stdout + res.stderr
        
        found = False
        for line in logs.split('\n'):
            if "Langfuse trace exported successfully" in line and req_id in line:
                print(f"\n✅ SUCCESS: Found telemetry success confirmation in worker logs!")
                print(f"   Log: {line.strip()}")
                found = True
                break
        
        if not found:
            print("\n❌ FAILED: Telemetry success message not found in latest worker logs.")
            
except urllib.error.HTTPError as e:
    print(f"    <-- HTTP ERROR {e.code}: {e.read().decode()}")
except Exception as e:
    print(f"    <-- EXCEPTION: {e}")

print("\n" + "="*60)
print("  Test complete.")
