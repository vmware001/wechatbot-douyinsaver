"""Does the labeler favor bigger base at similar share/digg ratio?

For each labeled pair where the two videos' share/digg ratios are close
(|log ratio diff| < 0.35) but the base (digg) differs >= 2x, count how often
the bigger-base video won. Ties excluded (uninformative here).
"""
import json
import math
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
manifest = json.loads((BASE / "data" / "douyin_saved" / "manifest.json")
                      .read_text(encoding="utf-8"))
records = manifest.get("videos") or {}

labels = []
with open(BASE / "data" / "label_pool" / "labels.jsonl", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line:
            labels.append(json.loads(line))

def info(iid):
    st = (records.get(iid) or {}).get("stats") or {}
    digg = int(st.get("digg", 0) or 0)
    share = int(st.get("share", 0) or 0)
    ratio = (share / digg) if digg > 0 else 0.0
    return digg, ratio

tested = big_won = 0
examples = []
for l in labels:
    if l["winner"] == "tie":
        continue
    da, ra = info(l["a"])
    db, rb = info(l["b"])
    if da < 3 or db < 3 or ra <= 0 or rb <= 0:
        continue
    if abs(math.log(ra / rb)) >= 0.35:
        continue
    base_ratio = max(da, db) / max(1, min(da, db))
    if base_ratio < 2.0:
        continue
    big = "a" if da > db else "b"
    tested += 1
    won = l["winner"] == big
    big_won += won
    if len(examples) < 8:
        examples.append((da, ra, db, rb, big, l["winner"], won))

print(f"pairs with similar ratio but >=2x base gap: {tested}")
print(f"bigger-base video won: {big_won} ({big_won/max(tested,1):.0%})")
for da, ra, db, rb, big, winner, won in examples:
    print(f"  A[digg={da} ratio={ra:.2f}] vs B[digg={db} ratio={rb:.2f}] "
          f"-> winner={winner} ({'big' if won else 'small'} won)")
