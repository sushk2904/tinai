import json, time, urllib.request, subprocess

URL = 'http://localhost:8000/v1/infer'
API_KEY = '5Yp3dNkVwY47Qq8BCsmv1KlNep7VguL0Qs375l4a1QE'
PROVIDER = 'groq'

def req():
    r = urllib.request.Request(URL, method='POST', headers={'Content-Type': 'application/json', 'x-api-key': API_KEY}, data=json.dumps({'prompt':'Write a haiku ' + str(time.time()), 'provider':PROVIDER, 'policy':'cost-first'}).encode('utf-8'))
    with urllib.request.urlopen(r) as resp: return json.loads(resp.read().decode())

subprocess.run(['docker', 'exec', 'tinai-redis-1', 'redis-cli', '-n', '0', 'DEL', f'pricing:multiplier:{PROVIDER}'], capture_output=True)

resp1 = req()
print('--- BASELINE ---')
print(f'Tokens: {resp1.get("token_count")}')
print(f'Cost  : {resp1.get("cost_cents")} cents')

tokens_b = resp1.get('token_count')
cost_b = resp1.get('cost_cents')

print(f'Baseline computed cost / token: {cost_b / tokens_b:.8f} cents' if tokens_b else '')

subprocess.run(['docker', 'exec', 'tinai-api-1', 'python', '-c', 'from workers.tasks.price_feed import simulate_price_update; simulate_price_update.delay()'], capture_output=True)
time.sleep(3)

mult = subprocess.run(['docker', 'exec', 'tinai-redis-1', 'redis-cli', '-n', '0', 'GET', f'pricing:multiplier:{PROVIDER}'], capture_output=True, text=True).stdout.strip()
if not mult: mult = "1.0"

resp2 = req()
tokens_s = resp2.get('token_count')
cost_s = resp2.get('cost_cents')

print(f'\n--- SURGE ---')
print(f'Multiplier in Redis: {mult}')
print(f'Tokens: {tokens_s}')
print(f'Cost  : {cost_s} cents')
print(f'Surge computed cost / token: {cost_s / tokens_s:.8f} cents' if tokens_s else '')

if float(mult) != 1.0 and tokens_s and tokens_b:
    actual_mult = (cost_s / tokens_s) / (cost_b / tokens_b)
    print(f'\nActual Effective Multiplier observed from Inference API: {actual_mult:.2f}x')

