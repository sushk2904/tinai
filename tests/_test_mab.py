import urllib.request
import json
import time

URL = "http://localhost:8000/v1/infer"

def get_routing(p_name, pol):
    req_data = json.dumps({
        "prompt": p_name,
        "policy": pol
    }).encode('utf-8')
    req = urllib.request.Request(URL, data=req_data, headers={
        'Content-Type': 'application/json',
        'x-api-key': '5Yp3dNkVwY47Qq8BCsmv1KlNep7VguL0Qs375l4a1QE'
    })
    start = time.time()
    with urllib.request.urlopen(req) as response:
        result = json.loads(response.read().decode())
        return result['provider'], result['latency_ms']

print("=== MAB Routing check ===")
print("Sending latency-first:")
print(get_routing("MAB routing test 1", "latency-first"))

print("Sending cost-first:")
print(get_routing("MAB routing test 2", "cost-first"))

print("Sending sla-aware:")
print(get_routing("MAB routing test 3", "sla-aware"))

