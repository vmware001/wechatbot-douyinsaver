"""Fit the Bradley-Terry curiosity model on the pairwise labels and report.

Usage: .venv/Scripts/python.exe tools/bt_fit.py

Splits labels 80/20 (deterministic shuffle), fits on train, reports pairwise
accuracy + Kendall tau of induced scores on held-out pairs, prints feature
weights (standardized space — magnitude = importance, sign = direction), and
writes bt_weights.json next to the labels for later deployment as the
download/merge scorer.
"""

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bt_model

FEATURE_NAMES = bt_model.FEATURE_NAMES


def pairwise_accuracy(pool, w, mean, std, labels):
    ok = tot = 0
    for l in labels:
        sa = bt_model.score(w, mean, std, pool[l["a"]]["x"])
        sb = bt_model.score(w, mean, std, pool[l["b"]]["x"])
        pred = "a" if sa > sb else ("b" if sb > sa else "tie")
        if l["winner"] == "tie":
            ok += 1 if abs(sa - sb) < 0.3 else 0
        else:
            ok += 1 if pred == l["winner"] else 0
        tot += 1
    return ok, tot


def main():
    pool = bt_model.load_pool()
    labels = bt_model.load_labels()
    usable = [l for l in labels if l["a"] in pool and l["b"] in pool]
    print(f"pool: {len(pool)} videos, labels: {len(labels)} ({len(usable)} usable)")
    if len(usable) < 20:
        print("need at least ~20 usable pairs for a meaningful fit; keep labeling")
        return 1

    rnd = random.Random(42)
    shuffled = list(usable)
    rnd.shuffle(shuffled)
    cut = max(1, int(len(shuffled) * 0.8))
    train, test = shuffled[:cut], shuffled[cut:]

    w, mean, std = bt_model.fit(pool, train)
    ok, tot = pairwise_accuracy(pool, w, mean, std, test)
    print(f"train pairs: {len(train)}, test pairs: {tot}")
    print(f"held-out pairwise accuracy: {ok}/{tot} = {ok / max(tot, 1):.1%}")

    print("\nfeature weights (standardized; + = more curious):")
    for name, wi in sorted(zip(FEATURE_NAMES, w), key=lambda t: -abs(t[1])):
        print(f"  {name:12s} {wi:+.4f}")

    # refit on ALL labels for the deployable weights
    w_all, mean, std = bt_model.fit(pool, usable)
    out = {"weights": w_all, "mean": mean, "std": std,
           "features": FEATURE_NAMES, "trained_on": len(usable)}
    bt_model.WEIGHTS_PATH.write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nweights written to {bt_model.WEIGHTS_PATH}")

    # current pool leaderboard, for eyeballing
    ranked = sorted(pool.items(),
                    key=lambda kv: -bt_model.score(w_all, mean, std, kv[1]["x"]))
    print("\npool top 10 by learned score:")
    for iid, r in ranked[:10]:
        print(f"  {bt_model.score(w_all, mean, std, r['x']):+.3f}  "
              f"ratio={r['ratio']:.2f}  {(r['title'] or '')[:40]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
