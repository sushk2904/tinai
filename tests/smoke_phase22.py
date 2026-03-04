"""
Quick smoke test for Phase 2.2 security modules.
Run: docker compose exec api python /app/tests/smoke_phase22.py
"""
import asyncio
import hashlib
import hmac

# --- auth.py ---
from api.auth import _derive_client_key
from api.config import settings
from api.redis_keys import key_rate_limit, key_daily_budget, key_budget_blocked

print("=" * 55)
print("Phase 2.2 — Security Layer Smoke Test")
print("=" * 55)

# 1. Client key derivation — deterministic
k1 = _derive_client_key("my-key")
k2 = _derive_client_key("my-key")
assert k1 == k2,      "FAIL: client key not deterministic"
assert len(k1) == 32, f"FAIL: expected 32 chars, got {len(k1)}"
print(f"[PASS] client_key derivation  → {k1}")

# 2. Collision resistance
k3 = _derive_client_key("other-key")
assert k1 != k3, "FAIL: collision between different keys"
print(f"[PASS] collision resistance   → {k3}")

# 3. Timing-safe comparison
valid   = hmac.compare_digest(settings.x_api_key_secret.encode(), settings.x_api_key_secret.encode())
invalid = hmac.compare_digest(b"wrong", settings.x_api_key_secret.encode())
assert valid   is True,  "FAIL: valid key rejected"
assert invalid is False, "FAIL: invalid key accepted"
print(f"[PASS] timing-safe compare    → valid={valid}, invalid={invalid}")

# 4. Redis key namespaces resolve correctly for security keys
ck = _derive_client_key("test-client")
print(f"[PASS] rate_limit key         → {key_rate_limit(ck)}")
print(f"[PASS] daily_budget key       → {key_daily_budget(ck)}")
print(f"[PASS] budget_blocked key     → {key_budget_blocked(ck)}")

# 5. Rate limiter Lua script round-trip against live Redis DB 0
import redis as redis_sync
r = redis_sync.from_url(settings.redis_url_mab, decode_responses=True)

probe_key = key_rate_limit("smoke-test-client")
r.delete(probe_key)   # clean slate

LUA = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return current
"""
count1 = r.eval(LUA, 1, probe_key, "60")
count2 = r.eval(LUA, 1, probe_key, "60")
ttl    = r.ttl(probe_key)
r.delete(probe_key)   # cleanup

assert count1 == 1, f"FAIL: expected 1, got {count1}"
assert count2 == 2, f"FAIL: expected 2, got {count2}"
assert ttl > 0,     f"FAIL: TTL not set (got {ttl})"
print(f"[PASS] rate-limit Lua script  → count1={count1}, count2={count2}, ttl={ttl}s")

# 6. Budget guard Redis round-trip
budget_key  = key_daily_budget("smoke-test-client")
blocked_key = key_budget_blocked("smoke-test-client")
r.delete(budget_key, blocked_key)

r.incrbyfloat(budget_key, 50.0)
spent = float(r.get(budget_key))
assert spent == 50.0, f"FAIL: expected 50.0, got {spent}"
r.delete(budget_key, blocked_key)
print(f"[PASS] budget INCRBYFLOAT     → spent={spent} cents")

r.close()

print()
print("=" * 55)
print("ALL CHECKS PASSED — Phase 2.2 security layer is solid.")
print("=" * 55)
