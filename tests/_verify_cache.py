import httpx
import time
import sys

URL = "http://localhost:8000/v1/infer"
HEADERS = {
    "Content-Type": "application/json",
    "x-api-key": "5Yp3dNkVwY47Qq8BCsmv1KlNep7VguL0Qs375l4a1QE"
}
PAYLOAD = {
    "prompt": "What is the capital of France? One word only.",
    "policy": "latency-first"
}

def p(msg):
    print(msg, flush=True)

try:
    p("=== Req 1: Expect Cache MISS ===")
    r1 = httpx.post(URL, headers=HEADERS, json=PAYLOAD, timeout=10.0)
    data1 = r1.json()
    p(f"Cache Hit: {data1.get('cache_hit')}, Latency: {data1.get('latency_ms')}ms")

    p("\nWaiting 2 seconds for Celery task to populate redis...")
    time.sleep(2.0)

    p("=== Req 2: Expect Cache HIT ===")
    r2 = httpx.post(URL, headers=HEADERS, json=PAYLOAD, timeout=10.0)
    data2 = r2.json()
    p(f"Cache Hit: {data2.get('cache_hit')}, Latency: {data2.get('latency_ms')}ms")
    
except Exception as e:
    p(f"ERROR: {e}")
