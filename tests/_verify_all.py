import urllib.request
import urllib.error
import json
import time

URL = "http://localhost:8000/v1/infer"

def make_request(prompt: str, policy: str = "latency-first", wait_after_ms: int = 0):
    req_data = json.dumps({
        "prompt": prompt,
        "policy": policy
    }).encode('utf-8')
    req = urllib.request.Request(URL, data=req_data, headers={
        'Content-Type': 'application/json',
        'x-api-key': '5Yp3dNkVwY47Qq8BCsmv1KlNep7VguL0Qs375l4a1QE' # Known key
    })

    start = time.time()
    try:
        with urllib.request.urlopen(req) as response:
            result = json.loads(response.read().decode())
            hit = result.get('cache_hit')
            latency = result.get('latency_ms')
            provider = result.get('provider')
            print(f"[{policy}] hit={hit:<5} | latency={latency:<4}ms | provider={provider:<10} ({prompt})")
    except urllib.error.URLError as e:
        print(f"[ERR] connection failed for prompt: {prompt}. Error: {e}")

    if wait_after_ms > 0:
        print(f"Waiting {wait_after_ms/1000.0}s ...")
        time.sleep(wait_after_ms / 1000.0)

if __name__ == '__main__':
    print("=== L1 Cache Strict Verification ===")
    prompt = "Is Asia the biggest continent?"
    make_request(prompt, wait_after_ms=4000)
    make_request(prompt)
    
    print("\n=== MAB Provider / Policy Verification ===")
    make_request("Tell me about cheap things.", policy="cost-first", wait_after_ms=1000)
    make_request("Tell me about reliable things.", policy="sla-aware", wait_after_ms=1000)
    make_request("Tell me about fast things.", policy="latency-first", wait_after_ms=1000)
