import urllib.request
import json
import time
import sys

URL = "http://localhost:8000/v1/infer"

def get_hit(p_name):
    req_data = json.dumps({
        "prompt": p_name,
        "policy": "latency-first"
    }).encode('utf-8')
    req = urllib.request.Request(URL, data=req_data, headers={
        'Content-Type': 'application/json',
        'x-api-key': '5Yp3dNkVwY47Qq8BCsmv1KlNep7VguL0Qs375l4a1QE'
    })
    start = time.time()
    try:
        with urllib.request.urlopen(req) as response:
            result = json.loads(response.read().decode())
            return result['cache_hit'], result['latency_ms']
    except Exception as e:
        return str(e), -1

hit1, lat1 = get_hit("Final cache proof test")
time.sleep(3)
hit2, lat2 = get_hit("Final cache proof test")

with open('result.json', 'w') as f:
    json.dump({
        "req_1": {"hit": hit1, "latency": lat1},
        "req_2": {"hit": hit2, "latency": lat2}
    }, f)
