import urllib.request
import json
import time

URL = "http://localhost:8000/v1/infer"

def fire_request():
    req = urllib.request.Request("http://localhost:8000/v1/infer", data=repr({"prompt": "Is Asia the biggest continent", "policy": "latency-first"}).replace("'", '"').encode(), headers={"Content-Type": "application/json", "x-api-key": "5Yp3dNkVwY47Qq8BCsmv1:KlNep7..."}, method="POST")
    # Using correct API key
    req = urllib.request.Request("http://localhost:8000/v1/infer", data=b'{"prompt": "Is Asia the biggest continent", "policy": "latency-first"}', headers={"Content-Type": "application/json", "x-api-key": "5Yp3dNkVwY47Qq8BCsmv1KlNep7VguL0Qs375l4a1QE"}, method="POST")
    start = time.time()
    try:
        with urllib.request.urlopen(req) as response:
            result = json.loads(response.read().decode())
            return result['cache_hit'], result['latency_ms']
    except Exception as e:
        return str(e), -1

print("--- Request 1: Should be MISS ---")
hit1, lat1 = fire_request()
print(f"Hit: {hit1}, Latency: {lat1}ms")

print("Waiting 3.5 seconds...")
time.sleep(3.5)

print("--- Request 2: Should be HIT ---")
hit2, lat2 = fire_request()
print(f"Hit: {hit2}, Latency: {lat2}ms")
