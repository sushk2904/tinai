import time
import requests

API_URL = "http://localhost:8000/v1/infer"
API_KEY = "5Yp3dNkVwY47Qq8BCsmv1KlNep7VguL0Qs375l4a1QE"

hdrs = {
    'Content-Type': 'application/json',
    'x-api-key': API_KEY
}
payload = {
    "prompt": "Is Asia the biggest continent",
    "policy": "latency-first"
}

print("=== Sending Request 1 (Expect MISS) ===", flush=True)
try:
    r1 = requests.post(API_URL, json=payload, headers=hdrs, timeout=15)
    d1 = r1.json()
    print(f"Server reported cache_hit: {d1.get('cache_hit')}")
    print(f"Network Latency (Total execution): {d1.get('latency_ms')} ms")
except Exception as e:
    print("Error connecting:", e)

print("\n--- Waiting 4 seconds for Celery to populate Redis ---", flush=True)
time.sleep(4)

print("\n=== Sending Request 2 (Expect HIT) ===", flush=True)
try:
    r2 = requests.post(API_URL, json=payload, headers=hdrs, timeout=15)
    d2 = r2.json()
    print(f"Server reported cache_hit: {d2.get('cache_hit')}")
    print(f"Network Latency (Total execution): {d2.get('latency_ms')} ms")
except Exception as e:
    print("Error connecting:", e)

