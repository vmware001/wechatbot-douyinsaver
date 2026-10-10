"""Bradley-Terry model with covariates for the curiosity labeling pipeline.

Pairwise labels ("A is weirder than B" / tie) fit a linear scorer over
log-count features:

    score(v) = w . x(v)        x = standardized [log1p(digg), log1p(comment),
                               log1p(collect), log1p(share), log1p(duration_s)]
    P(A > B) = sigmoid(score(A) - score(B))

Fitting is plain gradient descent on the pairwise log-likelihood (ties count
as a half win for each side). Pure stdlib + no numpy: a few hundred pairs x
5 features x a few hundred iterations is trivially fast in pure Python.

Shared by tools/douyin_console.py (uncertainty sampling for the next pair)
and tools/bt_fit.py (offline fit + report).
"""

import json
import math
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
# 视频库根目录来自 DOUYIN_LIB_DIR 环境变量，默认 D:\douyin_lib
# （旧版本曾硬编码 1TB D 盘路径；C:\GSMPSWechatBot\data\{douyin_saved,label_pool} 留有软链接兜底）
POOL_DIR = Path(os.environ.get("DOUYIN_LIB_DIR", r"D:\douyin_lib")) / "label_pool"
LABELS_PATH = POOL_DIR / "labels.jsonl"
WEIGHTS_PATH = POOL_DIR / "bt_weights.json"
MANIFEST_PATH = Path(os.environ.get("DOUYIN_LIB_DIR", r"D:\douyin_lib")) / "douyin_saved" / "manifest.json"

N_FEATURES = 7

FEATURE_NAMES = ["log_digg", "log_comment", "log_collect", "log_share",
                 "at_rate", "kw_rate", "log_duration"]

# comment keywords signaling "this is weird": "@ 好友来看" is the comment
# section's equivalent of a share; the keyword list is group-slang driven
# (想退出了 = "so weird I want to leave"). Extend as new slang shows up.
CURIO_KEYWORDS = ("猎奇", "阴间", "什么鬼", "离谱", "抽象", "邪门", "逆天",
                  "神人", "何意味", "想退出", "诗人握持", "掉san", "掉SAN",
                  "太阴", "诡", "细思极恐", "怕了", "谁不怕", "哥几个")


def _comment_rates(rec):
    """(at_rate, kw_rate) from stored comments; None when never fetched.

    at_rate: average @-mentions per comment (tagging a friend ≈ sharing).
    kw_rate: fraction of comments hitting a curiosity keyword.
    An empty fetched list is real data (a dead comment section) -> 0.0.
    """
    if "comments" not in rec:
        return None, None
    comments = rec.get("comments") or []
    if not comments:
        return 0.0, 0.0
    ats = sum(str(c.get("text") or "").count("@") for c in comments)
    hits = sum(1 for c in comments
               if any(k in str(c.get("text") or "") for k in CURIO_KEYWORDS))
    n = len(comments)
    return ats / n, hits / n


def feature_vector(rec):
    """Raw (unstandardized) feature vector from a manifest record.

    Comment rates are None for records predating comment capture;
    _standardize imputes the pool mean (neutral after standardization).
    Comment-CENSORSHIP guard: high-digg videos whose comment section was
    wiped/hidden by official moderation (digg >= 2000 but <= 20 visible
    comments) would feed a fake "nobody reacts" signal — all comment-derived
    features go to None (imputed mean) instead.
    """
    st = rec.get("stats") or {}
    dur_s = (rec.get("duration_ms") or 0) / 1000.0
    digg = int(st.get("digg", 0) or 0)
    comment = int(st.get("comment", 0) or 0)
    censored = digg >= 2000 and comment <= 20
    at_rate, kw_rate = _comment_rates(rec)
    if censored:
        at_rate = None
        kw_rate = None
    return [
        math.log1p(digg),
        None if censored else math.log1p(comment),
        math.log1p(int(st.get("collect", 0) or 0)),
        math.log1p(int(st.get("share", 0) or 0)),
        at_rate,
        kw_rate,
        math.log1p(dur_s),
    ]


def load_pool(pool_dir=POOL_DIR, manifest_path=MANIFEST_PATH):
    """item_id -> {"x": raw features, "title", "author", "kind"}

    Only items whose media file actually sits in the pool dir AND that have
    a manifest record (features) are labelable. Videos pool as .mp4, image
    posts as their stitched .jpg. Image posts have no duration — that feature
    stays None so standardization imputes the pool mean (neutral).
    """
    pool_dir = Path(pool_dir)
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except Exception:
        manifest = {}
    records = manifest.get("videos") or {}
    pool = {}
    for p in [*pool_dir.glob("*.mp4"), *pool_dir.glob("*.jpg")]:
        iid = p.stem
        rec = records.get(iid)
        if not rec or not rec.get("stats"):
            continue
        x = feature_vector(rec)
        kind = str(rec.get("type") or "video")
        if kind == "images":
            x[-1] = None  # no meaningful duration for image posts
        pool[iid] = {
            "x": x,
            "title": rec.get("title") or "",
            "author": rec.get("author") or "",
            "ratio": rec.get("ratio") or 0.0,
            "kind": kind,
            # pool file form: image posts are pooled as a 2s-per-image video
            # (.mp4); legacy .jpg stitched files render as plain images
            "media": "video" if p.suffix == ".mp4" else "image",
        }
    return pool


def load_pool_for_fit(labels, pool=None, manifest_path=MANIFEST_PATH):
    """拟合/图表用池 = load_pool() + 媒体已被淘汰但标注还在的条目。

    特征全部来自 manifest（点赞/评论/收藏/转发/关键词/时长），与媒体文件无关；
    标注池容量淘汰会删旧媒体（2026-10-03 事故），但 labels.jsonl 的投票仍然
    有效 —— 拟合不该因为媒体没了就丢掉这些样本。UI 选样仍用 load_pool()，
    只展示媒体还在的条目。
    """
    pool = dict(pool if pool is not None else load_pool(manifest_path=manifest_path))
    need = set()
    for l in labels or []:
        need.add(str(l.get("a")))
        need.add(str(l.get("b")))
    missing = [i for i in need if i and i not in pool]
    if not missing:
        return pool
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except Exception:
        manifest = {}
    records = manifest.get("videos") or {}
    for iid in missing:
        rec = records.get(iid)
        if not rec or not rec.get("stats"):
            continue
        x = feature_vector(rec)
        if str(rec.get("type") or "") == "images":
            x[-1] = None
        pool[iid] = {
            "x": x,
            "title": rec.get("title") or "",
            "author": rec.get("author") or "",
            "ratio": rec.get("ratio") or 0.0,
            "kind": str(rec.get("type") or "video"),
            "media": "missing",
        }
    return pool


def load_labels(labels_path=LABELS_PATH):
    """List of {"a", "b", "winner": "a"|"b"|"tie", "time"}."""
    labels = []
    try:
        with open(labels_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("winner") in ("a", "b", "tie"):
                    labels.append(rec)
    except OSError:
        pass
    return labels


def append_label(a, b, winner, labels_path=LABELS_PATH, who="me"):
    path = Path(labels_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    from datetime import datetime
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "a": a, "b": b, "winner": winner, "who": who,
            "time": datetime.now().isoformat(timespec="seconds"),
        }, ensure_ascii=False) + "\n")


def _standardize(pool):
    """Return {iid: standardized x} plus (mean, std) for later reuse.

    Missing features (None) are imputed with the pool mean, i.e. they become
    0 after standardization — neutral for the linear score.
    """
    ids = list(pool.keys())
    mean = []
    std = []
    for j in range(N_FEATURES):
        vals = [pool[iid]["x"][j] for iid in ids if pool[iid]["x"][j] is not None]
        m = sum(vals) / len(vals) if vals else 0.0
        v = sum((x - m) ** 2 for x in vals) / len(vals) if vals else 0.0
        mean.append(m)
        std.append(math.sqrt(v) or 1.0)
    xs = {}
    for iid in ids:
        xs[iid] = [((pool[iid]["x"][j] if pool[iid]["x"][j] is not None else mean[j])
                    - mean[j]) / std[j]
                   for j in range(N_FEATURES)]
    return xs, mean, std


def fit(pool, labels, iters=400, lr=0.5, l2=1e-3):
    """Gradient descent on pairwise log-likelihood.

    Returns (weights, mean, std); scores elsewhere must standardize with the
    same mean/std. Labels referencing items no longer in the pool (evicted)
    are skipped.
    """
    xs, mean, std = _standardize(pool)
    w = [0.0] * N_FEATURES
    usable = [l for l in labels if l["a"] in xs and l["b"] in xs]
    if not usable:
        return w, mean, std
    for _ in range(iters):
        grad = [0.0] * N_FEATURES
        for l in usable:
            xa, xb = xs[l["a"]], xs[l["b"]]
            diff = [xa[j] - xb[j] for j in range(N_FEATURES)]
            s = sum(w[j] * diff[j] for j in range(N_FEATURES))
            sig = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, s))))
            if l["winner"] == "a":
                coeff = 1.0 - sig
            elif l["winner"] == "b":
                coeff = 0.0 - sig
            else:  # tie: half win each way -> pulls diff toward 0
                coeff = 0.5 - sig
            for j in range(N_FEATURES):
                grad[j] += coeff * diff[j]
        n = len(usable)
        for j in range(N_FEATURES):
            grad[j] = grad[j] / n - l2 * w[j]
            w[j] += lr * grad[j]
    return w, mean, std


def score(weights, mean, std, raw_x):
    return sum(weights[j] * ((raw_x[j] if raw_x[j] is not None else mean[j])
                             - mean[j]) / std[j]
               for j in range(N_FEATURES))


def pair_nll(pool, labels, weights, mean, std):
    """平均负对数似然（每对投票的交叉熵）——越小越贴合投票。

    用作"有没有收敛"的判据：权重可能还在平坦谷底漂，但损失不再下降
    就说明拟合到位了（2026-10-03 实测 Δw 不降而 Δloss 稳定趋零）。
    """
    tot = 0.0
    n = 0
    for l in labels:
        if l.get("winner") not in ("a", "b"):
            continue
        try:
            s = (score(weights, mean, std, pool[l["a"]]["x"])
                 - score(weights, mean, std, pool[l["b"]]["x"]))
        except Exception:
            continue
        p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, s))))
        p = p if l["winner"] == "a" else 1.0 - p
        tot += -math.log(max(1e-9, p))
        n += 1
    return tot / max(1, n)


def compared_pairs(labels):
    return {tuple(sorted((l["a"], l["b"]))) for l in labels}
