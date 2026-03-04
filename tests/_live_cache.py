import urllib.request
import json
import time

URL = "http://localhost:8000/v1/infer"

def fire_request(prompt_text):
    req_data = json.dumps({
        "prompt": prompt_text,
        "policy": "latency-first"
    }).encode('utf-8')
    req = urllib.request.Request(URL, data=req_data, headers={
        'Content-Type': 'application/json',
        'x-api-key': '5Yp3dNkVwY47Qq8BCsmv1KlNep7VguL0Qs375l4a1QE'
    })
    start = time.time()
    with urllib.request.urlopen(req) as response:
        result = json.loads(response.read().decode())
        return result['cache_hit'], result['latency_ms']

PROMPT = "Explain the universe in 3 words."

print("=== Req 1: New Prompt ===")
hit, lat = fire_request(PROMPT)
print(f"Cache Hit: {hit}, Latency: {lat}ms")

print("Waiting 3 seconds for Celery...")
time.sleep(3)

print("=== Req 2: Same Prompt ===")
hit, lat = fire_request(PROMPT)
print(f"Cache Hit: {hit}, Latency: {lat}ms")

