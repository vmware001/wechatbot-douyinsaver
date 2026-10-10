"""Exposure-invariance experiment: raw log-counts vs explicit ratio features.

The user's point: counts conflate content quality with reach (play volume).
In log space, scaling all counts by exposure E shifts the score by
log(E) * sum(weights). A weight sum of zero => fully exposure-invariant.

Compare feature sets over repeated random 80/20 splits:
  A: raw log counts + duration            (current production)
  B: ratios only (share/digg, comment/digg, collect/digg) + duration
  C: ratios + log_digg (reach proxy) + duration
  D: raw counts + ratios + duration       (kitchen sink)
"""
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bt_model

pool = bt_model.load_pool()
manifest = json_load = None
import json
manifest = json.loads((Path(__file__).resolve().parent.parent
                       / "data" / "douyin_saved" / "manifest.json")
                      .read_text(encoding="utf-8"))
records = manifest.get("videos") or {}

labels = [l for l in bt_model.load_labels()
          if l["a"] in pool and l["b"] in pool]
print(f"pool={len(pool)} labels={len(labels)}")


def feats(iid, mode):
    st = (records.get(iid) or {}).get("stats") or {}
    digg = int(st.get("digg", 0) or 0)
    cmt = int(st.get("comment", 0) or 0)
    col = int(st.get("collect", 0) or 0)
    shr = int(st.get("share", 0) or 0)
    dur = ((records.get(iid) or {}).get("duration_ms") or 0) / 1000.0
    counts = [math.log1p(digg), math.log1p(cmt), math.log1p(col),
              math.log1p(shr)]
    ratios = [math.log((shr + 1) / (digg + 1)),
              math.log((cmt + 1) / (digg + 1)),
              math.log((col + 1) / (digg + 1))]
    d = [math.log1p(dur)]
    if mode == "A":
        return counts + d
    if mode == "B":
        return ratios + d
    if mode == "C":
        return ratios + [math.log1p(digg)] + d
    if mode == "D":
        return counts + ratios + d


def run(mode, trials=60):
    orig_n = bt_model.N_FEATURES
    bt_model.N_FEATURES = len(feats(labels[0]["a"], mode))
    sp = {iid: {**r, "x": feats(iid, mode)} for iid, r in pool.items()}
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
                    ok += 1 if ("a" if sa > sb else "b") == l["winner"] else 0
                tot += 1
            if tot:
                accs.append(ok / tot)
    finally:
        bt_model.N_FEATURES = orig_n
    return sum(accs) / len(accs)


for mode, name in [("A", "raw counts+dur (current)"),
                   ("B", "ratios+dur"),
                   ("C", "ratios+reach+dur"),
                   ("D", "counts+ratios+dur")]:
    print(f"{mode} {name:28s} mean held-out acc: {run(mode):.1%}")
