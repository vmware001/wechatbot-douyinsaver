"""Study: comment censorship + pre-filter selection bias.

Q1: some videos have censored comment sections (official moderation):
    high digg, near-zero comments. Their at_rate/kw_rate are artifacts,
    not signal. Detect via comment/digg outliers, neutralize, refit.

Q2: the label pool was pre-filtered by ratio>=0.5 (share/digg) at download
    time. Does that selection distort the learned weights? Simulate stricter
    thresholds on the labeled set and compare refits.

Console output is English-only.
"""
import json
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import bt_model as bt

BASE = Path(__file__).resolve().parent.parent
MANIFEST = BASE / "data" / "douyin_saved" / "manifest.json"

pool = bt.load_pool()
labels = bt.load_labels()
usable = [l for l in labels if l["a"] in pool and l["b"] in pool]
names = bt.FEATURE_NAMES

manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
records = manifest.get("videos") or {}


def counts(iid):
    st = (records.get(iid) or {}).get("stats") or {}
    return int(st.get("digg", 0) or 0), int(st.get("comment", 0) or 0)


def show(tag, w):
    print("  %-26s %s" % (tag, "  ".join(f"{n}={x:+.3f}" for n, x in zip(names, w))))


def acc_pairs(w, pairs, pool_):
    """pairwise accuracy; tie counts 0.5 correct."""
    _, mean, std = bt._standardize(pool_)
    good = 0.0
    for l in pairs:
        sa = bt.score(w, mean, std, pool_[l["a"]]["x"])
        sb = bt.score(w, mean, std, pool_[l["b"]]["x"])
        if l["winner"] == "tie":
            good += 1.0 - min(1.0, abs(sa - sb))
        elif (l["winner"] == "a") == (sa > sb):
            good += 1.0
    return good / max(1, len(pairs))


print("== Q2: selection bias — refit under stricter ratio thresholds ==")
w0, _, _ = bt.fit(pool, usable)
show(f"all pairs (n={len(usable)})", w0)
print("  baseline acc:", round(acc_pairs(w0, usable, pool), 3))
for T in (1.0, 1.5, 2.0, 3.0, 5.0):
    sub = [l for l in usable
           if pool[l["a"]]["ratio"] >= T and pool[l["b"]]["ratio"] >= T]
    if len(sub) < 30:
        print(f"  ratio>={T}: only {len(sub)} pairs, skipped")
        continue
    w, _, _ = bt.fit(pool, sub)
    show(f"both ratio>={T} (n={len(sub)})", w)

print()
print("== Q1: comment censorship ==")
# comment/digg ratio distribution for pool items with meaningful digg
rows = []
for iid in pool:
    digg, comment = counts(iid)
    if digg >= 500:
        rows.append((comment / max(digg, 1), iid, digg, comment))
rows.sort()
print(f"  pool items with digg>=500: {len(rows)}")
qs = [rows[int(q * (len(rows) - 1))] for q in (0.05, 0.25, 0.5, 0.75, 0.95)]
print("  comment/digg percentiles:",
      "  ".join(f"p{int(q*100)}={r[0]:.4f}" for r, q in zip(qs, (0.05, 0.25, 0.5, 0.75, 0.95))))

# suspects: digg>=2000 AND comment/digg below the 5th percentile-ish cutoff
cut = max(0.002, rows[int(0.05 * (len(rows) - 1))][0]) if rows else 0.002
suspects = {iid for ratio, iid, digg, comment in rows
            if digg >= 2000 and ratio <= cut}
labeled_ids = {l["a"] for l in usable} | {l["b"] for l in usable}
sus_labeled = suspects & labeled_ids
print(f"  cutoff comment/digg <= {cut:.4f} with digg>=2000")
print(f"  suspects in pool: {len(suspects)}, of which labeled: {len(sus_labeled)}")
for iid in sorted(sus_labeled):
    digg, comment = counts(iid)
    r = pool[iid]
    print(f"    {iid} digg={digg} comment={comment} ratio={r['ratio']} "
          f"kw={r['x'][5]} at={r['x'][4]}")

if sus_labeled:
    # variant A: drop pairs touching a suspect
    subA = [l for l in usable if l["a"] not in suspects and l["b"] not in suspects]
    wA, _, _ = bt.fit(pool, subA)
    show(f"A: drop pairs (n={len(subA)})", wA)
    print("  acc:", round(acc_pairs(wA, subA, pool), 3))
    # variant B: neutralize comment features of suspects (impute pool mean)
    poolB = {iid: dict(r) for iid, r in pool.items()}
    for iid in suspects:
        x = list(poolB[iid]["x"])
        x[4] = None
        x[5] = None
        poolB[iid]["x"] = x
    wB, _, _ = bt.fit(poolB, usable)
    show(f"B: neutralize (n={len(usable)})", wB)
    print("  acc:", round(acc_pairs(wB, usable, poolB), 3))
