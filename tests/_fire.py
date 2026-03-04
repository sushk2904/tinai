import urllib.request
import json
import time

URL = "http://localhost:8000/v1/infer"

def get_hit(p_name):
    req_data = json.dumps({
        "prompt": p_name,
        "policy": "cost-first"
    }).encode('utf-8')
    req = urllib.request.Request(URL, data=req_data, headers={
        'Content-Type': 'application/json',
        'x-api-key': '5Yp3dNkVwY47Qq8BCsmv1KlNep7VguL0Qs375l4a1QE'
    })
    start = time.time()
    with urllib.request.urlopen(req) as response:
        result = json.loads(response.read().decode())
        return result['cache_hit'], result['latency_ms']

print("Sending Prompt: 'Explain MAB'")
hit1, lat1 = get_hit("Explain MAB")
print(f"Req 1 -> Cache Hit: {hit1}, Latency: {lat1}ms")
time.sleep(3)
print("Sending Prompt: 'Explain MAB' Again")
hit2, lat2 = get_hit("Explain MAB")
print(f"Req 2 -> Cache Hit: {hit2}, Latency: {lat2}ms")
