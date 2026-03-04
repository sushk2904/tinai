import urllib.request
import json
import time

URL = "http://localhost:8000/v1/infer"
headers = {
    'Content-Type': 'application/json',
    'x-api-key': '5Yp3dNkVwY47Qq8BCsmv1KlNep7VguL0Qs375l4a1QE'
}
data = json.dumps({
    "prompt": "Is Asia the biggest continent?",
    "policy": "latency-first"
}).encode()

print("--> Sending Request 1 (New Prompt)")
start = time.time()
with urllib.request.urlopen(urllib.request.Request(URL, data=data, headers=headers)) as res:
    resp1 = json.loads(res.read().decode())
    print(f"Request 1 | Cache Hit: {resp1.get('cache_hit')} | Latency: {int((time.time()-start)*1000)}ms")

print("--> Waiting 3.5s for Celery Background Populator...")
time.sleep(3.5)

print("--> Sending Request 2 (Identical Prompt)")
start = time.time()
with urllib.request.urlopen(urllib.request.Request(URL, data=data, headers=headers)) as res:
    resp2 = json.loads(res.read().decode())
    print(f"Request 2 | Cache Hit: {resp2.get('cache_hit')} | Latency: {int((time.time()-start)*1000)}ms")
