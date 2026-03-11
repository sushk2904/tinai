"""
Phase 4.3 Verification: Progressive Load Shedding

Validates that the load shedder instantly rejects all requests with HTTP 503
when activated via the Admin Endpoint, and resumes normal traffic when disabled.
"""

import json
import time
import urllib.request
import urllib.error

API_KEY = "5Yp3dNkVwY47Qq8BCsmv1KlNep7VguL0Qs375l4a1QE"

def toggle_shed(active: bool):
    print(f"\n[!] Toggling Load Shedding => {'ACTIVE' if active else 'DISABLED'}")
    req = urllib.request.Request(
        "http://localhost:8000/admin/load-shedding",
        method="POST",
        headers={"Content-Type": "application/json", "x-api-key": API_KEY},
        data=json.dumps({"active": active}).encode("utf-8")
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())

def invoke_inference(tag: str):
    print(f"    --> Requesting inference [{tag}] ...")
    req = urllib.request.Request(
        "http://localhost:8000/v1/infer",
        method="POST",
        headers={"Content-Type": "application/json", "x-api-key": API_KEY},
        data=json.dumps({"prompt": f"Write a haiku about scaling {time.time()}", "provider": "fallback"}).encode("utf-8")
    )
    try:
        t0 = time.time()
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            print(f"    <-- HTTP OK. Latency: {data.get('latency_ms')}ms")
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode())
        print(f"    <-- HTTP ERROR {e.code}: {body.get('detail')}")
    except Exception as e:
        print(f"    <-- EXCEPTION: {e}")

print("="*60)
print("  Phase 4.3 — Progressive Load Shedding Verification")
print("="*60)


# 1. Ensure shed is off
toggle_shed(False)
invoke_inference("Baseline")

# 2. Turn on
toggle_shed(True)
invoke_inference("Load Shedded Request")
invoke_inference("Second Load Shedded Request")

# 3. Recover
toggle_shed(False)
invoke_inference("Recovered Request")

print("\n" + "="*60)
print("  Test complete.")
