import json
from pathlib import Path
from collections import defaultdict

d = json.loads(Path("tests/batch_results_scored.json").read_text(encoding="utf-8-sig"))

print(f"\n{'='*65}")
print(f"{'#':>2}  {'Provider':<12} {'Quality':>7}  {'Latency':>8}  {'Cost':>8}  Rationale")
print(f"{'='*65}")
for r in d:
    print(f"{r['Num']:>2}. {r['Provider']:<12} {r['QualityScore']:>7.3f}  {r['LatencyMs']:>6}ms  {float(r['CostC']):>8.4f}c  {r['Rationale'][:50]}")

print()
by_provider = defaultdict(list)
for r in d:
    by_provider[r["Provider"]].append(r["QualityScore"])

print("Average Quality by Provider:")
for p, scores in sorted(by_provider.items(), key=lambda x: -sum(x[1])/len(x[1])):
    avg = sum(scores) / len(scores)
    print(f"  {p:<14} avg={avg:.3f}  n={len(scores)}")
print()
