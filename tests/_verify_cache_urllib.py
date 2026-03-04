import urllib.request
import json
import time

URL = "http://localhost:8000/v1/infer"
REQ_JSON = json.dumps({
    "prompt": "Test cache routing",
    "policy": "latency-first"
}).encode('utf-8')

req = urllib.request.Request(URL, data=REQ_JSON, headers={
    'Content-Type': 'application/json',
    'x-api-key': '5Yp3dNkVwY47Qq8BCsmv1KlNep7VguL0Qs375l4a1QE'
})

print("=== Req 1: Expect Cache MISS ===")
start = time.time()
with urllib.request.urlopen(req) as response:
    result = json.loads(response.read().decode())
    print(f"Cache Hit: {result['cache_hit']}, Server Latency: {result['latency_ms']}ms, Python latency: {int((time.time()-start)*1000)}ms")

print("Waiting 2.5s for Celery...")
time.sleep(2.5)

print("=== Req 2: Expect Cache HIT ===")
start = time.time()
with urllib.request.urlopen(req) as response:
    result = json.loads(response.read().decode())
    print(f"Cache Hit: {result['cache_hit']}, Server Latency: {result['latency_ms']}ms, Python latency: {int((time.time()-start)*1000)}ms")
