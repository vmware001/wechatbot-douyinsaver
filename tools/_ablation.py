"""Ablation: does log_age_hours help or hurt? Repeated random 80/20 splits,
mean held-out pairwise accuracy for different feature subsets."""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bt_model

pool = bt_model.load_pool()
labels = [l for l in bt_model.load_labels()
          if l["a"] in pool and l["b"] in pool]
print(f"pool={len(pool)} labels={len(labels)}")

# feature indices: 0 digg, 1 comment, 2 collect, 3 share, 4 at_rate,
# 5 kw_rate, 6 duration
SUBSETS = {
    "counts4+duration (no comments)": [0, 1, 2, 3, 6],
    "counts4+dur+comments (current)": [0, 1, 2, 3, 4, 5, 6],
}


def sub_pool(pool, keep):
    return {iid: {**r, "x": [r["x"][j] for j in keep]}
            for iid, r in pool.items()}


def run(keep, trials=60):
    orig_n = bt_model.N_FEATURES
    bt_model.N_FEATURES = len(keep)
    sp = sub_pool(pool, keep)
    accs = []
    try:
        for seed in range(trials):
            rnd = random.Random(seed)
            sh = list(labels)
            rnd.shuffle(sh)
            cut = max(1, int(len(sh) * 0.8))
            train, test = sh[:cut], sh[cut:]
            w, mean, std = bt_model.fit(sp, train)
            ok = tot = 0
            for l in test:
                sa = bt_model.score(w, mean, std, sp[l["a"]]["x"])
                sb = bt_model.score(w, mean, std, sp[l["b"]]["x"])
                if l["winner"] == "tie":
                    ok += 1 if abs(sa - sb) < 0.3 else 0
                else:
                    pred = "a" if sa > sb else "b"
                    ok += 1 if pred == l["winner"] else 0
                tot += 1
            if tot:
                accs.append(ok / tot)
    finally:
        bt_model.N_FEATURES = orig_n
    return sum(accs) / len(accs)


for name, keep in SUBSETS.items():
    print(f"{name:24s} mean held-out acc over 60 splits: {run(keep):.1%}")
