import json
from pathlib import Path
from collections import Counter

d = json.loads(Path("tests/batch_results.json").read_text(encoding="utf-8-sig"))
if isinstance(d, dict):
    d = [d]

print("COUNT:", len(d))
print()
print(f"{'#':>2}  {'Policy':<14} {'Provider':<12} {'Latency':>8}  {'Cost':>8}")
print("-" * 55)
for r in d:
    print(f"{r['Num']:>2}. {r['Policy']:<14} {r['Provider']:<12} {r['LatencyMs']:>6}ms  {float(r['CostC']):>8.4f}c")

print()
cnt = Counter(r["Provider"] for r in d)
print("Provider Distribution:")
for p, n in cnt.most_common():
    print(f"  {p:<12} {n:>2} calls ({int(n/len(d)*100)}%)")

print()
ok = [r for r in d if r["Provider"] != "ERROR"]
print("Avg stats by Provider:")
for p in sorted(set(r["Provider"] for r in ok)):
    rows = [r for r in ok if r["Provider"] == p]
    avg_lat  = int(sum(r["LatencyMs"] for r in rows) / len(rows))
    avg_cost = sum(float(r["CostC"]) for r in rows) / len(rows)
    print(f"  {p:<12} latency={avg_lat:>6}ms  cost={avg_cost:.5f}c  n={len(rows)}")

print()
print("Policy -> Provider:")
for pol in ["sla-aware", "latency-first", "cost-first"]:
    rows = [r for r in ok if r["Policy"] == pol]
    cnt2 = Counter(r["Provider"] for r in rows)
    print(f"  {pol:<14} -> {dict(cnt2)}")
