"""Web dashboard for the douyin saver + forwarder pipeline.

Serves a single-page console on http://127.0.0.1:9998/ showing:
  - download status (per-video stats + save/skip decision, from the saver log)
  - send status (forwarder log: sent / failed / holding / next burst)
  - current local queue (mp4s waiting to be forwarded)
  - gate states (quiet hours, chat suppression, thresholds)

Also serves /label: a pairwise curiosity-labeling page over the label pool
(writes data/label_pool/labels.jsonl). Never touches the browser or WeChat.
"""
import queue  # stdlib preload
import hashlib
import json
import os
import random
import re
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import bt_model

BASE_DIR = Path(__file__).resolve().parent.parent
# 视频库根目录来自 DOUYIN_LIB_DIR 环境变量，默认 D:\douyin_lib
SAVE_ROOT = Path(os.environ.get("DOUYIN_LIB_DIR", r"D:\douyin_lib")) / "douyin_saved"
SAVER_LOG = SAVE_ROOT / "douyin_saver.log"
FORWARDER_LOG = SAVE_ROOT / "douyin_forwarder.log"
SAVER_CFG = BASE_DIR / "config" / "douyin_saver.json"
FORWARDER_CFG = BASE_DIR / "config" / "douyin_forwarder.json"
PORT = 9998

STATS_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[^\n]*STATS (\w+)(?: id=(\d+))? "
    r"digg=(\d+) share=(\d+) "
    r"ratio=([\d.]+)(?: author=\[(.*?)\])? -> (SAVED|stats-only|DUP)")
SEND_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[^\n]*(INFO|WARNING|ERROR) (.*)$")

GATE_PATTERNS = ("quiet hours", "group is chatting", "queue empty", "pausing", "gave up")

# douyin fills this in when a video is shared without a custom message:
# "@xxx发了一个抖音短视频，你尽管点开，不好看算我输！" — group 1 is the author
DEFAULT_SHARE_RE = re.compile(r"^@(.+?)发了一个抖音短视频")

# Public labeling access (cloudflare tunnel): visitors on this host get ONLY
# the labeling page + media, and their votes go to a separate file so they
# never mix into the model's training labels.
PUBLIC_HOST = os.environ.get("DOUYIN_PUBLIC_HOST", "label.example.com")
GUEST_LABELS = bt_model.POOL_DIR / "labels_guest.jsonl"
GUEST_PREFIXES = ("/label", "/api/label/")


def tail_lines(path: Path, max_lines=400, tail_bytes=200_000):
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - tail_bytes))
            text = f.read().decode("utf-8", "replace")
        return text.splitlines()[-max_lines:]
    except OSError:
        return []


def collect_status():
    saver_cfg = json.loads(SAVER_CFG.read_text(encoding="utf-8"))
    fwd_cfg = json.loads(FORWARDER_CFG.read_text(encoding="utf-8"))

    # file path -> stats lookup from the manifest (for queue ratios)
    file_stats = {}
    manifest = {}
    try:
        manifest = json.loads((SAVE_ROOT / "manifest.json").read_text(encoding="utf-8"))
        for rec in manifest.get("videos", {}).values():
            st = rec.get("stats") or {}
            ratio = rec.get("ratio")
            # 时长：视频取 duration_ms；图文按 图数×2s 估算（与拼接一致）
            dm = rec.get("duration_ms") or 0
            dur = dm / 1000.0
            if not dur:
                n_img = len([f for f in (rec.get("files") or [])
                             if Path(f).name.startswith("img_")])
                dur = n_img * 2.0
            for f in rec.get("files", []):
                file_stats[str(Path(f).resolve())] = {
                    "digg": st.get("digg", 0), "share": st.get("share", 0),
                    "ratio": ratio, "dur": round(dur, 1), "rec": rec,
                }
    except Exception:
        pass

    today = datetime.now().strftime("%Y-%m-%d")

    rows = []
    saved_today = 0
    dups_today = 0
    # the poll/pass lines drown out STATS rows in a short tail; read deeper
    for line in tail_lines(SAVER_LOG, max_lines=6000, tail_bytes=2_000_000):
        m = STATS_RE.match(line)
        if not m:
            continue
        ts, kind, iid, digg, share, ratio, author, decision = m.groups()
        if ts.startswith(today):
            if decision == "SAVED":
                saved_today += 1
            elif decision == "DUP":
                dups_today += 1
        rows.append({
            "time": ts[11:19], "kind": kind, "id": iid,
            "digg": int(digg), "share": int(share),
            "ratio": float(ratio), "author": author or "-", "decision": decision,
            # 已下载但文件已不在发送池 = 已经拼进合集发出去了（发完即删）
            "on_disk": any(Path(f).exists()
                           for f in (((manifest.get("videos") or {}).get(iid or "")
                                      or {}).get("files") or [])),
        })
    # one row per aweme id — the latest occurrence wins; older restarts'
    # re-logs of the same card collapse away. Legacy log lines have no id;
    # fall back to a content signature so pre-fix bursts also collapse.
    # Historical logs contain false DUP rows (the original card re-logged
    # after sitting in view for 10 min) — when merging, a SAVED/stats-only
    # line for the same item overrides a DUP decision.
    prio = {"SAVED": 0, "stats-only": 1, "DUP": 2}
    downloads = []
    seen_ids = {}
    seen_content = {}
    for row in reversed(rows):
        # ratio is logged with different precision in DUP vs original lines
        # (2.333 vs 2.33), so it must stay out of the content signature
        ckey = (row["kind"], row["digg"], row["share"], row["author"])
        ikey = row["id"] or None
        idx = seen_ids.get(ikey) if ikey else None
        if idx is None:
            idx = seen_content.get(ckey)
        if idx is not None:
            if prio.get(row["decision"], 3) < prio.get(downloads[idx]["decision"], 3):
                downloads[idx]["decision"] = row["decision"]
            # adopt the id once any occurrence carries one, so later
            # id-bearing lines keep merging into this row
            if ikey and not downloads[idx]["id"]:
                downloads[idx]["id"] = ikey
                seen_ids[ikey] = idx
            continue
        idx = len(downloads)
        if ikey:
            seen_ids[ikey] = idx
        seen_content[ckey] = idx
        downloads.append(row)

    # resolve leftover id-less DUP rows against the manifest: their original
    # STATS line is older than the log tail, but the manifest still holds the
    # item's true status. A DUP row is only ever emitted for an item the
    # saver has already processed, so an unmatched one is a historical false
    # positive (the original card re-logged after sitting in view). Genuine
    # re-shares logged after the uuid fix carry an id and skip this.
    stat_index = {}
    for iid, rec in (manifest.get("videos") or {}).items():
        st = rec.get("stats") or {}
        stat_index.setdefault((st.get("digg"), st.get("share")), []).append((iid, rec))
    for row in downloads:
        if row["decision"] != "DUP" or row["id"]:
            continue
        cand = stat_index.get((row["digg"], row["share"])) or []
        if len(cand) != 1:
            continue
        iid, rec = cand[0]
        row["id"] = iid
        row["decision"] = "SAVED" if rec.get("downloaded") else "stats-only"
        row["on_disk"] = any(Path(f).exists() for f in (rec.get("files") or []))

    sends = []
    sent_today = 0
    state = "idle"
    for line in tail_lines(FORWARDER_LOG):
        m = SEND_RE.match(line)
        if not m:
            continue
        ts, level, text = m.groups()
        if ts.startswith(today) and "sent and deleted" in text:
            sent_today += 1
        if any(p in text for p in GATE_PATTERNS) or level in ("ERROR", "WARNING"):
            sends.append({"time": ts[11:19], "level": level, "text": text[:160]})
            if "quiet hours" in text:
                state = "quiet hours"
            elif "group is chatting" in text:
                state = "chat hold"
            elif "queue empty" in text:
                state = "waiting (burst cycle)"
        elif "sent and deleted" in text:
            sends.append({"time": ts[11:19], "level": level, "text": text[:160]})
    sends.reverse()
    if not sends:
        state = "no recent forwarder activity"

    watch_dir = Path(fwd_cfg["watch_dir"])
    # merge gate: download threshold is wider than the merge threshold on
    # purpose (extra videos feed the labeling pool); the queue must show
    # only what collect_items() would actually pick, so apply the same gate.
    merge_min_ratio = float(fwd_cfg.get("merge_min_ratio", 2.0))
    merge_min_digg = int(fwd_cfg.get("merge_min_digg", 5))
    queue_files = []
    mp = model_params()
    if watch_dir.exists():
        now = time.time()
        cands = sorted([p for p in [*watch_dir.rglob("*.mp4"), *watch_dir.rglob("stitched.jpg")]
                        if "image_debug" not in p.parts],
                       key=lambda x: x.stat().st_mtime)
        for p in cands:
            st = file_stats.get(str(p.resolve())) or {}
            ratio = st.get("ratio")
            digg = st.get("digg", 0)
            if (ratio or 0.0) < merge_min_ratio or (digg or 0) < merge_min_digg:
                continue
            if p.name == "stitched.jpg":
                name = "[图文] " + p.parent.name
            else:
                name = p.stem[7:] or p.stem  # strip HHMMSS_ prefix
            # 猎奇指数 = 投票后模型分（与标注页同一套权重）；算不出显示 null
            score = None
            if mp and st.get("rec"):
                try:
                    score = round(float(bt_model.score(
                        mp[0], mp[1], mp[2],
                        bt_model.feature_vector(st["rec"]))), 3)
                except Exception:
                    score = None
            queue_files.append({
                "name": name, "mb": round(p.stat().st_size / 1e6, 1),
                "age_min": int((now - p.stat().st_mtime) // 60),
                "digg": digg, "share": st.get("share", 0),
                "ratio": ratio, "dur": st.get("dur", 0), "score": score,
            })

    quiet = fwd_cfg.get("quiet_hours") or {}
    now = datetime.now()
    qstart, qend = str(quiet.get("start", "")), str(quiet.get("end", ""))
    t = now.strftime("%H:%M")
    in_quiet = bool(qstart and qend and (qstart <= t < qend if qstart <= qend
                                         else t >= qstart or t < qend))

    return {
        "now": now.strftime("%Y-%m-%d %H:%M:%S"),
        "saver": {
            "group": "、".join(saver_cfg.get("group_names")
                               or [saver_cfg["group_name"]]),
            "threshold": saver_cfg["download_ratio_threshold"],
            "min_digg": saver_cfg.get("download_min_digg", 10),
            "saved_today": saved_today,
            "dups_today": dups_today,
            "recent": downloads[:30],
        },
        "forwarder": {
            "target": fwd_cfg["target"],
            "budget": fwd_cfg.get("max_compilation_seconds", 300),
            "burst": fwd_cfg["burst_interval_seconds"],
            "merge_min_ratio": merge_min_ratio,
            "merge_min_digg": merge_min_digg,
            "quiet": f"{qstart}-{qend}",
            "in_quiet": in_quiet,
            "chat_hold": fwd_cfg.get("chat_hold", {}).get("enabled", False),
            "sent_today": sent_today,
            "state": state,
            "queue": queue_files,
            "recent": sends[:40],
        },
    }


# =========================================================
# pairwise curiosity labeling (Bradley-Terry, see bt_model.py)
# =========================================================

# weight-trajectory cache: recomputing ~40 prefix fits on every poll is
# wasteful; the trajectory only changes when a new label lands.
_traj_cache = {"n_labels": -1, "n_pool": -1, "data": None}
_traj_cache_guest = {"n_labels": -1, "n_pool": -1, "data": None}
_scatter_cache = {"n_labels": -1, "n_pool": -1, "data": None}
_model_cache = {"key": None, "data": None}


def model_params():
    """当前模型的 (weights, mean, std)；拟合一次、按标注数缓存。

    待发送队列的"猎奇指数"列用它给每条视频打分（与标注页同一套模型）。
    标注不足 5 对时返回 None（调用方显示 '—'）。
    """
    labels = bt_model.load_labels()
    pool = bt_model.load_pool_for_fit(labels)
    key = (len(labels), len(pool))
    if _model_cache["key"] == key and _model_cache["data"]:
        return _model_cache["data"]
    usable = [l for l in labels if l["a"] in pool and l["b"] in pool]
    if len(usable) < 5:
        return None
    try:
        w, mean, std = bt_model.fit(pool, usable, iters=400)
    except Exception:
        return None
    _model_cache.update(key=key, data=(w, mean, std))
    return _model_cache["data"]


def label_weight_trajectory(labels_path=None, cache=None):
    """How each BT weight evolved as labels accumulated (chronological).

    Fits the model on the first k labels for ~40 sampled k values, so the
    /label page can plot convergence: flat curves = enough data.
    """
    labels_path = labels_path or bt_model.LABELS_PATH
    cache = cache if cache is not None else _traj_cache
    labels = bt_model.load_labels(labels_path)
    # 拟合池含"媒体已被淘汰但标注还在"的条目（特征来自 manifest）
    pool = bt_model.load_pool_for_fit(labels)
    usable = [l for l in labels if l["a"] in pool and l["b"] in pool]
    cache_key = (len(labels), len(pool))
    if cache["n_labels"] == cache_key[0] and \
            cache["n_pool"] == cache_key[1] and cache["data"] is not None:
        return cache["data"]

    min_k = 10
    max_points = 40
    ks = list(range(min_k, len(usable) + 1))
    if len(ks) > max_points:
        step = len(ks) / max_points
        ks = sorted({ks[int(i * step)] for i in range(max_points)}
                    | ({len(usable)} if len(usable) >= min_k else set()))
    points = []
    for k in ks:
        w, mean, std = bt_model.fit(pool, usable[:k], iters=150)
        points.append({"k": k, "w": [round(x, 3) for x in w]})
    data = {"names": bt_model.FEATURE_NAMES, "points": points,
            "total": len(usable)}
    cache.update(n_labels=cache_key[0], n_pool=cache_key[1], data=data)
    return data


def label_scatter(labels_path=None, cache=None):
    """散点图数据：x = 平凡猎奇指数（转发/点赞），y = 投票标记后模型算出的猎奇指数。

    每个点 = 一个被人工标注过（进过对比对）的视频。y 用全体标注重新 fit 出的
    权重打分（score = w·标准化特征，越大越猎奇）；x 是老的简单比值。
    两者的散点关系能看出"投票到底学到了多少比值之外的信息"。
    """
    labels_path = labels_path or bt_model.LABELS_PATH
    cache = cache if cache is not None else _scatter_cache
    labels = bt_model.load_labels(labels_path)
    pool = bt_model.load_pool_for_fit(labels)
    usable = [l for l in labels if l["a"] in pool and l["b"] in pool]
    cache_key = (len(labels), len(pool))
    if cache.get("n_labels") == cache_key[0] and \
            cache.get("n_pool") == cache_key[1] and cache.get("data") is not None:
        return cache["data"]
    data = {"points": [], "n": 0, "names": bt_model.FEATURE_NAMES}
    if len(usable) >= 5:
        # 拟合两次（200 / 400 轮）看损失还在不在降 —— Δloss<0.005 视为收敛。
        # 权重本身可能一直在平坦谷底漂（Δw 不为零），但预测已稳定，所以
        # 收敛判据用损失而不是权重变化（2026-10-03 实测）。
        w200, mean, std = bt_model.fit(pool, usable, iters=200)
        w, _, _ = bt_model.fit(pool, usable, iters=400)
        l200 = bt_model.pair_nll(pool, usable, w200, mean, std)
        l400 = bt_model.pair_nll(pool, usable, w, mean, std)
        conv_dloss = round(max(0.0, l200 - l400), 5)
        agree = n_pairs = 0
        for l in usable:
            if l.get("winner") not in ("a", "b"):
                continue
            try:
                sa = bt_model.score(w, mean, std, pool[l["a"]]["x"])
                sb = bt_model.score(w, mean, std, pool[l["b"]]["x"])
            except Exception:
                continue
            n_pairs += 1
            pred = "a" if sa > sb else ("b" if sa < sb else "")
            if pred == l["winner"]:
                agree += 1
        labeled = set()
        for l in usable:
            labeled.add(l["a"])
            labeled.add(l["b"])
        pts = []
        for iid in labeled:
            rec = pool.get(iid) or {}
            try:
                x = float(rec.get("ratio") or 0.0)
                y = float(bt_model.score(w, mean, std, rec.get("x") or []))
            except Exception:
                continue
            pts.append({"x": round(x, 4), "y": round(y, 3),
                        "id": iid, "author": (rec.get("author") or "")[:8],
                        "has_media": str(rec.get("media") or "") != "missing"})
        pts.sort(key=lambda p: p["x"])
        data = {"points": pts, "n": len(pts),
                "weights": [round(v, 3) for v in w],
                "conv_dloss": conv_dloss,
                "converged": bool(conv_dloss < 0.005),
                "agree_pct": round(100.0 * agree / max(1, n_pairs), 1),
                "n_pairs": n_pairs}
    cache.update(n_labels=cache_key[0], n_pool=cache_key[1], data=data)
    return data


def label_next(labels_path=None, guest_stats=False):
    """Pick the next pair to label.

    labels_path selects the labeler's own history: my labels drive my model,
    guests get a separate file so their `done` set and counts are their own.
    guest_stats=True (local page only) also reports how many votes friends
    have cast, broken down per visitor hash.

    Priority order:
      1. pairs involving never-labeled items (fresh pool arrivals first —
         the labeler should not keep re-seeing old videos while new ones wait)
      2. >=10 labels: uncertainty sampling among adjacent-in-score pairs
      3. any uncompared pair at random
    Exact-repeat pairs are never served (the `done` set). Within each tier,
    pairs are weighted by 1/((1+uses_a)*(1+uses_b)): the more a video has
    already been labeled, the less likely it is to come up again, so labels
    spread across the pool instead of piling onto a few items.
    """
    pool = bt_model.load_pool()
    labels = bt_model.load_labels(labels_path or bt_model.LABELS_PATH)
    done = bt_model.compared_pairs(labels)
    ids = sorted(pool.keys())
    out = {"labeled": len(labels), "pool": len(ids)}
    if len(ids) < 2:
        out["pair"] = None
        return out

    def uncompared(pairs):
        return [(a, b) for a, b in pairs
                if tuple(sorted((a, b))) not in done]

    usage = {i: 0 for i in ids}
    for l in labels:
        if l["a"] in usage:
            usage[l["a"]] += 1
        if l["b"] in usage:
            usage[l["b"]] += 1

    def weighted_pick(cand):
        weights = [1.0 / ((1 + usage[a]) * (1 + usage[b])) for a, b in cand]
        total = sum(weights)
        r = random.random() * total
        acc = 0.0
        for p, w in zip(cand, weights):
            acc += w
            if r <= acc:
                return p
        return cand[-1]

    pair = None
    # 1. fresh items first: pair each never-labeled item with a random other
    fresh = [i for i in ids if usage[i] == 0]
    if fresh:
        random.shuffle(fresh)
        for f in fresh:
            others = [i for i in ids if i != f]
            random.shuffle(others)
            cand = uncompared([(f, o) for o in others])
            if cand:
                pair = cand[0]
                break

    if pair is None and len(labels) >= 10:
        w, mean, std = bt_model.fit(pool, labels)
        scored = sorted(ids,
                        key=lambda i: bt_model.score(w, mean, std, pool[i]["x"]))
        adjacent = [(scored[i], scored[i + 1]) for i in range(len(scored) - 1)]
        cand = uncompared(adjacent)
        if cand:
            # any of the closest 30 keeps a bit of exploration;
            # low-usage items are favored by the weighting
            pair = weighted_pick(cand[:30])
    if pair is None:
        all_pairs = [(ids[i], ids[j])
                     for i in range(len(ids)) for j in range(i + 1, len(ids))]
        cand = uncompared(all_pairs)
        if not cand:
            out["pair"] = None
            return out
        pair = weighted_pick(cand)

    def view(iid):
        r = pool[iid]
        # stats deliberately hidden: the label must judge the content,
        # not the counts the model will later learn from
        title = r["title"][:80]
        m = DEFAULT_SHARE_RE.match(title)
        if m:
            # douyin's auto share copy carries no info beyond the author name
            title = m.group(1) or r["author"]
        return {"id": iid, "title": title, "author": r["author"],
                "media": r.get("media") or "video"}

    if guest_stats:
        guests = bt_model.load_labels(GUEST_LABELS)
        per_who = {}
        for g in guests:
            per_who[g.get("who") or "?"] = per_who.get(g.get("who") or "?", 0) + 1
        out["guest"] = {"total": len(guests),
                        "voters": sorted(per_who.values(), reverse=True)}

    out["pair"] = {"a": view(pair[0]), "b": view(pair[1])}
    return out


LABEL_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>猎奇标注</title>
<style>
  body { font-family: "Microsoft YaHei", sans-serif; background: #1e1e24; color: #ddd;
         margin: 10px; font-size: 13px; }
  h1 { font-size: 15px; margin: 4px 0; }
  .stat { color: #9fd; margin-bottom: 8px; }
  .row { display: flex; gap: 10px; }
  .side { flex: 1; background: #2a2a33; border-radius: 6px; padding: 8px; }
  video { width: 100%; max-height: 62vh; background: #000; border-radius: 4px; }
  .t { font-size: 12px; color: #bbb; margin-top: 6px; word-break: break-all; }
  .btns { margin: 12px 0; display: flex; gap: 10px; justify-content: center; }
  button { font-size: 15px; padding: 10px 22px; border: none; border-radius: 6px;
           cursor: pointer; background: #3a3a46; color: #eee; }
  button:hover { background: #4a4a5a; }
  .keys { color: #777; font-size: 11px; text-align: center; }
  a { color: #9fd; }
</style>
</head>
<body>
<h1>猎奇标注 <a href="/">← 控制台</a></h1>
<div class="stat" id="stat">加载中…</div>
<div class="row">
  <div class="side"><div id="ma"></div><div class="t" id="ta"></div></div>
  <div class="side"><div id="mb"></div><div class="t" id="tb"></div></div>
</div>
<div class="btns">
  <button onclick="choose('a')">← 左边更猎奇</button>
  <button onclick="choose('tie')">差不多</button>
  <button onclick="choose('b')">右边更猎奇 →</button>
</div>
<div class="keys">快捷键：← 左边 / ↓ 差不多 / → 右边。只看视频内容判断，别看数据。</div>
<!--WEIGHTS_BEGIN-->
<h2 style="font-size:13px;margin:14px 0 4px;color:#9fd">权重收敛曲线（每新标一对自动更新）</h2>
<div style="display:flex;gap:16px;align-items:flex-start">
<div id="wchart">加载中…</div>
<div class="keys" id="wlegend" style="text-align:left;line-height:2.0;padding-top:8px"></div>
</div>
<h2 style="font-size:13px;margin:14px 0 4px;color:#9fd">好友权重曲线（好友标注独立拟合，不进模型）</h2>
<div style="display:flex;gap:16px;align-items:flex-start">
<div id="gchart">加载中…</div>
<div class="keys" id="glegend" style="text-align:left;line-height:2.0;padding-top:8px"></div>
</div>
<!--WEIGHTS_END-->
<script>
let cur = null;
async function next() {
  const r = await (await fetch('/api/label/next')).json();
  let stat = '已标注 ' + r.labeled + ' 对 / 标注池 ' + r.pool + ' 个作品';
  if (r.guest) {
    stat += ' ｜ 好友标注 ' + r.guest.total + ' 次';
    if (r.guest.voters.length)
      stat += '（' + r.guest.voters.length + ' 人：各 '
        + r.guest.voters.join(' / ') + ' 次）';
  }
  document.getElementById('stat').textContent = stat;
  if (!r.pair) {
    document.getElementById('stat').textContent += ' —— 暂无更多可标注的对';
    cur = null;
    return;
  }
  cur = r.pair;
  for (const [boxId, item] of [['ma', r.pair.a], ['mb', r.pair.b]]) {
    const box = document.getElementById(boxId);
    box.innerHTML = item.media === 'image'
      ? `<img src="/label/media/${item.id}" style="width:100%;max-height:62vh;object-fit:contain;border-radius:4px">`
      : `<video controls src="/label/media/${item.id}"></video>`;
  }
  document.getElementById('ta').textContent = r.pair.a.title;
  document.getElementById('tb').textContent = r.pair.b.title;
}
async function choose(w) {
  if (!cur) return;
  await fetch('/api/label/choose', {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({a: cur.a.id, b: cur.b.id, winner: w})});
  for (const v of document.querySelectorAll('#ma video, #mb video')) v.pause();
  next();
  drawWeights();
}
document.addEventListener('keydown', e => {
  if (e.key === 'ArrowLeft') choose('a');
  else if (e.key === 'ArrowRight') choose('b');
  else if (e.key === 'ArrowDown') { e.preventDefault(); choose('tie'); }
});

const WCOLORS = ['#ff6b6b', '#5aa9ff', '#ffd479', '#7fd47f', '#d47fd4'];
const WNAMES = {'log_digg': '点赞', 'log_comment': '评论', 'log_collect': '收藏',
                'log_share': '转发', 'log_duration': '时长',
                'at_rate': '@密度', 'kw_rate': '关键词'};
async function drawWeights() {
  const box = document.getElementById('wchart');
  if (!box) return;  // guest page: the curve section is stripped server-side
  const r = await (await fetch('/api/label/weights')).json();
  if (!r.points) return;
  renderChart('wchart', 'wlegend', r.points, r.names);
  renderChart('gchart', 'glegend', r.guest_points || [], r.names);
}
function renderChart(boxId, legendId, points, names) {
  const box = document.getElementById(boxId);
  if (!points.length) { box.textContent = '标注满 10 对后开始显示'; return; }
  const W = 560, H = 220, ML = 42, MR = 10, MT = 10, MB = 25;
  const ks = points.map(p => p.k);
  const allW = points.flatMap(p => p.w);
  let lo = Math.min(...allW, 0), hi = Math.max(...allW, 0);
  if (hi - lo < 0.1) { hi += 0.1; lo -= 0.1; }
  const X = k => ML + (k - ks[0]) / Math.max(1, ks[ks.length - 1] - ks[0]) * (W - ML - MR);
  const Y = v => MT + (hi - v) / (hi - lo) * (H - MT - MB);
  let s = `<svg width="${W}" height="${H}" style="background:#2a2a33;border-radius:6px">`;
  s += `<line x1="${ML}" y1="${Y(0)}" x2="${W - MR}" y2="${Y(0)}" stroke="#555"/>`;
  s += `<text x="4" y="${Y(hi) + 8}" fill="#777" font-size="10">${hi.toFixed(1)}</text>`;
  s += `<text x="4" y="${Y(lo)}" fill="#777" font-size="10">${lo.toFixed(1)}</text>`;
  s += `<text x="${ML}" y="${H - 6}" fill="#777" font-size="10">${ks[0]}对</text>`;
  s += `<text x="${W - MR - 44}" y="${H - 6}" fill="#777" font-size="10">${ks[ks.length - 1]}对</text>`;
  names.forEach((_, j) => {
    const d = points.map((p, i) =>
      (i ? 'L' : 'M') + X(p.k).toFixed(1) + ' ' + Y(p.w[j]).toFixed(1)).join(' ');
    s += `<path d="${d}" fill="none" stroke="${WCOLORS[j % WCOLORS.length]}" stroke-width="1.8"/>`;
  });
  s += '</svg>';
  box.innerHTML = s;
  const last = points[points.length - 1];
  document.getElementById(legendId).innerHTML = names.map((n, j) =>
    `<span style="color:${WCOLORS[j % WCOLORS.length]}">■</span> ${WNAMES[n] || n} <b>${last.w[j].toFixed(3)}</b>`).join('<br>');
}
drawWeights();

next();
</script>
</body>
</html>"""


HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>抖音搬运控制台</title>
<style>
  body { font-family: "Microsoft YaHei", sans-serif; background: #1e1e24; color: #ddd;
         margin: 6px 8px; font-size: 11px; }
  h1 { font-size: 13px; margin: 2px 0 6px; } h2 { font-size: 11px; margin: 8px 0 3px; color: #9fd; }
  .cards { display: flex; gap: 5px; flex-wrap: wrap; }
  table { border-collapse: collapse; width: 100%; font-size: 10px; margin-top: 2px;
          table-layout: fixed; }
  td, th { padding: 1px 3px; border-bottom: 1px solid #33333c; text-align: left;
           white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
           max-width: 200px; }
  th { color: #777; font-weight: normal; }
  .SAVED { color: #7fd47f; font-weight: bold; }
  .stats-only { color: #777; }
  .ERROR { color: #ff7f7f; } .WARNING { color: #ffd479; } .INFO { color: #999; }
  .gate-on { color: #ff9f9f; font-weight: bold; }
  .gate-off { color: #7fd47f; }
  .dim { color: #777; font-size: 10px; font-weight: normal; }
  #gates ul { margin: 2px 0; padding-left: 14px; }
  .r { color: #ffd479; font-weight: bold; }
  .DUP { color: #d47fd4; font-weight: bold; }
  .hot { color: #ff6b6b; font-weight: bold; }
  .labelb { color: #5aa9ff; font-weight: bold; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 4px 10px; }
  @media (max-width: 860px) { .grid { grid-template-columns: 1fr; } }
  .card { background: #2a2a33; border-radius: 5px; padding: 3px 6px; min-width: 0;
          max-width: 190px; overflow: hidden; }
  .card .v { font-size: 12px; font-weight: bold; color: #ffd479; white-space: nowrap;
             overflow: hidden; text-overflow: ellipsis; }
  .card .k { font-size: 10px; color: #999; }
</style>
</head>
<body>
<h1>抖音搬运 <span class="dim" id="now"></span> <a href="/label" style="color:#9fd">猎奇标注 →</a></h1>

<div class="cards" id="cards"></div>

<div class="grid">
<div>
<h2>发送闸门</h2>
<div id="gates"></div>

<h2>待发送队列（<span id="qcount">0</span>）</h2>
<table id="queue"></table>
</div>
<div>
<h2>最新视频 <span style="color:#ff6b6b">■</span><span class="dim">将合并</span> <span style="color:#5aa9ff">■</span><span class="dim">仅标注</span></h2>
<table id="downloads"></table>

<h2>转发器动态</h2>
<table id="sends"></table>
</div>
</div>

<h2>猎奇指数对比（平凡指数 → 投票后模型指数）</h2>
<div style="display:flex;gap:16px;align-items:flex-start">
<div id="schart">加载中…</div>
<div id="sfit" class="dim" style="line-height:2.0;padding-top:6px;min-width:220px"></div>
</div>
<div id="svideo" style="margin-top:6px"></div>
<div class="dim" style="margin-top:3px;line-height:1.7">横轴：平凡猎奇指数 = 转发/点赞；纵轴：猎奇指数 = 10^模型分（两轴都是对数）。悬停看作者与数值，双击播放该视频。</div>

<script>
function renderScatter(boxId, pts, meta) {
  const box = document.getElementById(boxId);
  if (!box) return;
  const fit = document.getElementById('sfit');
  if (!pts.length) {
    box.textContent = '标注满 5 对后开始显示';
    if (fit) fit.textContent = '';
    return;
  }
  const W = 960, H = 400, ML = 52, MR = 16, MT = 18, MB = 34;
  const ys = pts.map(p => p.y);
  const lx = pts.map(p => Math.log10(Math.max(1e-3, p.x)));
  let xlo = Math.min(...lx), xhi = Math.max(...lx);
  let ylo = Math.min(...ys, 0), yhi = Math.max(...ys, 0);
  if (xhi - xlo < 0.3) { xhi += 0.15; xlo -= 0.15; }
  if (yhi - ylo < 0.3) { yhi += 0.15; ylo -= 0.15; }
  const X = v => ML + (Math.log10(Math.max(1e-3, v)) - xlo) / (xhi - xlo) * (W - ML - MR);
  const Y = v => MT + (yhi - v) / (yhi - ylo) * (H - MT - MB);
  // 回归：y = k·log10(x) + b（与图上的对数横轴一致，画出来才是直线）
  const n = lx.length;
  const sx = lx.reduce((a, b2) => a + b2, 0), sy = ys.reduce((a, b2) => a + b2, 0);
  const sxx = lx.reduce((a, v) => a + v * v, 0);
  const syy = ys.reduce((a, v) => a + v * v, 0);
  const sxy = lx.reduce((a, v, i) => a + v * ys[i], 0);
  const k = (n * sxy - sx * sy) / Math.max(1e-9, (n * sxx - sx * sx));
  const b = (sy - k * sx) / n;
  const r = (n * sxy - sx * sy) / Math.max(1e-9,
      Math.sqrt((n * sxx - sx * sx) * (n * syy - sy * sy)));
  let s = `<svg width="${W}" height="${H}" style="background:#2a2a33;border-radius:6px">`;
  s += `<line x1="${ML}" y1="${MT}" x2="${ML}" y2="${H - MB}" stroke="#555"/>`;
  s += `<line x1="${ML}" y1="${H - MB}" x2="${W - MR}" y2="${H - MB}" stroke="#555"/>`;
  // y 刻度：内部坐标是 log10(猎奇指数)，刻度标签按 10^v 显示（1/2/5 系列）
  const yTicks = [];
  for (let e = Math.floor(ylo); e <= Math.ceil(yhi); e++) {
    for (const m of [1, 2, 5]) {
      const v = e + Math.log10(m);
      if (v >= ylo && v <= yhi) yTicks.push([v, m * Math.pow(10, e)]);
    }
  }
  if (yTicks.length < 2) {
    yTicks.length = 0;
    [ylo, (ylo + yhi) / 2, yhi].forEach(v => yTicks.push([v, Math.pow(10, v)]));
  }
  yTicks.forEach(([v, label]) => {
    s += `<line x1="${ML}" y1="${Y(v)}" x2="${W - MR}" y2="${Y(v)}" stroke="#33333c"/>`;
    s += `<text x="3" y="${Y(v) + 3}" fill="#777" font-size="10">${label >= 10 ? label.toFixed(0) : label.toFixed(label >= 1 ? 1 : 2)}</text>`;
  });
  for (let e = Math.ceil(xlo); e <= Math.floor(xhi); e++) {
    const v = Math.pow(10, e);
    s += `<line x1="${X(v)}" y1="${MT}" x2="${X(v)}" y2="${H - MB}" stroke="#33333c"/>`;
    s += `<text x="${X(v) - 9}" y="${H - 12}" fill="#777" font-size="10">${v >= 1 ? v : v.toFixed(2)}</text>`;
  }
  // 回归线（橙色虚线）
  const yAt = v => k * Math.log10(Math.max(1e-3, v)) + b;
  s += `<line x1="${X(Math.pow(10, xlo))}" y1="${Y(yAt(Math.pow(10, xlo)))}" ` +
       `x2="${X(Math.pow(10, xhi))}" y2="${Y(yAt(Math.pow(10, xhi)))}" ` +
       `stroke="#ffb454" stroke-width="1.6" stroke-dasharray="6 4"/>`;
  pts.forEach(p => {
    const yi = Math.pow(10, p.y);
    s += `<circle cx="${X(p.x).toFixed(1)}" cy="${Y(p.y).toFixed(1)}" r="3.5" ` +
         `fill="#5aa9ff" fill-opacity="0.85" style="cursor:pointer" ` +
         `ondblclick="playVid('${p.id}', ${p.has_media ? 1 : 0}, '${(p.author || '').replace(/'/g, '')}')">` +
         `<title>${p.author}  平凡:${p.x}  投票:${yi >= 10 ? yi.toFixed(1) : yi.toFixed(2)}  (双击看视频)</title></circle>`;
  });
  s += `<text x="${ML + 4}" y="${MT + 10}" fill="#666" font-size="10">↑ 猎奇指数（10^模型分，对数轴）</text>`;
  s += `<text x="${W - MR - 150}" y="${H - 6}" fill="#666" font-size="10">平凡猎奇指数（转/赞，对数轴）→</text>`;
  s += '</svg>';
  box.innerHTML = s;
  if (fit) {
    const conv = (meta && meta.converged) ? '已收敛' : '仍在收敛中';
    const convColor = (meta && meta.converged) ? '#7fd47f' : '#ffd479';
    fit.innerHTML =
      `<div style="background:#2a2a33;border-radius:6px;padding:10px 14px;line-height:2.1">` +
      `<b style="color:#ffb454">回归线</b>　<span style="font-size:10px">橙色虚线即回归线</span><br>` +
      `猎奇指数 = <b>${Math.pow(10, b).toFixed(3)}</b> × 平凡指数^<b>${k.toFixed(3)}</b><br>` +
      `<span style="font-size:10px">（即 log₁₀指数 = k·log₁₀平凡 + b）</span><br>` +
      `k = <b style="color:#ffd479">${k.toFixed(3)}</b>　` +
      `b = <b style="color:#ffd479">${b.toFixed(3)}</b><br>` +
      `r = <b style="color:#ffd479">${r.toFixed(3)}</b>　` +
      `R² = <b style="color:#ffd479">${(r * r).toFixed(3)}</b><br>` +
      `一致率 = <b style="color:#ffd479">${(meta && meta.agree_pct != null) ? meta.agree_pct + '%' : '—'}</b>` +
      `<span style="font-size:10px">（模型排序 vs 投票）</span><br>` +
      `收敛：<b style="color:${convColor}">${conv}</b>　` +
      `<span style="font-size:10px">Δloss=${(meta && meta.conv_dloss != null) ? meta.conv_dloss : '—'}（200→400 轮）</span><br>` +
      `<span style="font-size:10px">样本 ${n} 点 / ${(meta && meta.n_pairs) || '—'} 对投票</span>` +
      `</div>`;
  }
}
function playVid(id, hasMedia, author) {
  const box = document.getElementById('svideo');
  if (!box) return;
  if (!hasMedia) {
    box.innerHTML = `<div class="dim" style="padding:4px">该视频媒体已淘汰（抖音侧也常拉不回），无法播放：${author} ${id}</div>`;
    return;
  }
  box.innerHTML = `<div style="display:flex;gap:10px;align-items:flex-start">
      <video controls autoplay style="max-height:260px;border-radius:6px;background:#000" src="/label/media/${id}"></video>
      <div class="dim" style="padding-top:4px">正在播放：${author}<br>${id}<br><a href="javascript:document.getElementById('svideo').innerHTML=''">关闭</a></div>
    </div>`;
}
async function drawScatter() {
  try {
    const r = await (await fetch('/api/label/scatter')).json();
    renderScatter('schart', r.points || [], r);
  } catch (e) {
    document.getElementById('schart').textContent = '加载失败';
  }
}
function fmtDur(s) {
  s = Number(s) || 0;
  if (s < 60) return Math.round(s) + 's';
  const m = Math.floor(s / 60), r = Math.round(s % 60);
  return m + ':' + String(r).padStart(2, '0');
}
async function refresh() {
  const s = await (await fetch('/api/status')).json();
  document.getElementById('now').textContent = s.now.slice(5, 16);
  const fw = s.forwarder, sv = s.saver;
  const cards = [
    ['监控群', sv.group], ['转发目标', fw.target],
    ['今日已存', sv.saved_today], ['今日已发', fw.sent_today],
    ['今日重复', sv.dups_today],
    ['下载阈值', '猎奇≥' + sv.threshold + ' 且 赞≥' + sv.min_digg],
    ['合并阈值', '猎奇≥' + fw.merge_min_ratio + ' 且 赞≥' + fw.merge_min_digg],
    ['节奏', '合并≤' + (fw.budget/60) + 'min/轮' + (fw.burst/3600) + 'h'],
  ];
  document.getElementById('cards').innerHTML = cards.map(c =>
    `<div class="card"><div class="v">${c[1]}</div><div class="k">${c[0]}</div></div>`).join('');
  const gates = [
    ['免打扰' + fw.quiet, fw.in_quiet ? '生效' : 'off', fw.in_quiet],
    ['聊天抑制', fw.chat_hold ? 'on' : 'off', false],
    ['状态', fw.state, false],
  ];
  document.getElementById('gates').innerHTML = '<ul>' + gates.map(g =>
    `<li class="${g[2] ? 'gate-on' : 'gate-off'}">${g[0]}：${g[1]}</li>`).join('') + '</ul>';
  document.getElementById('qcount').textContent = fw.queue.length;
  document.getElementById('queue').innerHTML =
    '<tr><th style="width:30%">视频</th><th style="width:15%">平凡猎奇指数</th><th style="width:13%">猎奇指数</th><th style="width:18%">转发/点赞</th><th style="width:10%">时长</th><th style="width:14%">等待时间</th></tr>' +
    fw.queue.map(q => `<tr><td title="${q.name}">${q.name.slice(0, 16)}</td>` +
      `<td class="dim">${q.ratio == null ? '?' : q.ratio}</td>` +
      `<td class="r">${q.score == null ? '—' : Math.pow(10, q.score).toFixed(2)}</td>` +
      `<td class="dim">${q.share}/${q.digg}</td>` +
      `<td>${fmtDur(q.dur)}</td><td>${q.age_min}m</td></tr>`).join('');
  document.getElementById('downloads').innerHTML =
    '<tr><th style="width:16%">时间</th><th style="width:11%">类型</th><th style="width:24%">转/赞</th><th style="width:13%">猎奇</th><th style="width:15%">结果</th><th style="width:21%">作者</th></tr>' +
    sv.recent.map(d => `<tr><td>${d.time}</td><td>${d.kind}</td>` +
      `<td>${d.share}/${d.digg}</td>` +
      `<td class="${d.decision === 'SAVED' ? (d.ratio >= fw.merge_min_ratio && d.digg >= fw.merge_min_digg ? 'hot' : 'labelb') : ''}">${d.ratio}</td><td class="${d.decision}">` +
      `${d.decision === 'SAVED' ? (d.on_disk ? '已下载' : '已发送') : d.decision === 'DUP' ? '重复' : '仅统计'}</td>` +
      `<td class="dim" title="${d.author}">${d.author === '-' ? '-' : d.author.slice(0, 8)}</td></tr>`).join('');
  document.getElementById('sends').innerHTML =
    '<tr><th style="width:15%">时间</th><th style="width:11%">级别</th><th style="width:74%">内容</th></tr>' +
    fw.recent.map(m => `<tr><td>${m.time}</td><td class="${m.level}">${m.level}</td><td title="${m.text}">${m.text.slice(0, 42)}</td></tr>`).join('');
}
refresh();
drawScatter();
setInterval(refresh, 10000);
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def _respond(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        self._respond(200, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                      "application/json; charset=utf-8")

    def _serve_media(self, item_id):
        # strict id shape: pool files are <aweme_id>.mp4 / .jpg; anything else
        # 404s (also blocks path traversal)
        if not re.fullmatch(r"\d{8,25}", item_id or ""):
            self._respond(404, b"not found", "text/plain; charset=utf-8")
            return
        path = bt_model.POOL_DIR / f"{item_id}.mp4"
        ctype = "video/mp4"
        if not path.exists():
            path = bt_model.POOL_DIR / f"{item_id}.jpg"
            ctype = "image/jpeg"
        try:
            size = path.stat().st_size
        except OSError:
            self._respond(404, b"not found", "text/plain; charset=utf-8")
            return
        start, end = 0, size - 1
        code = 200
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
                code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1 << 18, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)

    def _is_guest(self):
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        return host == PUBLIC_HOST

    def _label_html(self):
        if not self._is_guest():
            return LABEL_HTML.encode("utf-8")
        # guests never see the model internals: cut the weight curve section
        return re.sub(r"<!--WEIGHTS_BEGIN-->.*?<!--WEIGHTS_END-->", "",
                      LABEL_HTML, flags=re.S).encode("utf-8")

    def _labels_path(self):
        return GUEST_LABELS if self._is_guest() else bt_model.LABELS_PATH

    def do_GET(self):
        path = urlparse(self.path).path
        if self._is_guest():
            # guests get the labeling UI and nothing else (no console, no
            # status API); the bare domain lands on /label directly
            if path == "/":
                self._respond(200, self._label_html(),
                              "text/html; charset=utf-8")
            elif path == "/api/label/next":
                self._json(label_next(GUEST_LABELS))
            elif path.startswith("/label/media/"):
                self._serve_media(path.rsplit("/", 1)[-1])
            elif path == "/label":
                self._respond(200, self._label_html(),
                              "text/html; charset=utf-8")
            else:
                self._respond(404, b"not found", "text/plain; charset=utf-8")
            return
        if path.startswith("/api/status"):
            self._json(collect_status())
        elif path == "/api/label/next":
            self._json(label_next(guest_stats=True))
        elif path == "/api/label/weights":
            d = dict(label_weight_trajectory())
            g = label_weight_trajectory(GUEST_LABELS, _traj_cache_guest)
            d["guest_points"] = g["points"]
            d["guest_total"] = g["total"]
            self._json(d)
        elif path == "/api/label/scatter":
            self._json(label_scatter())
        elif path.startswith("/label/media/"):
            self._serve_media(path.rsplit("/", 1)[-1])
        elif path == "/label":
            self._respond(200, LABEL_HTML.encode("utf-8"),
                          "text/html; charset=utf-8")
        else:
            self._respond(200, HTML.encode("utf-8"), "text/html; charset=utf-8")

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/label/choose":
            self._respond(404, b"not found", "text/plain; charset=utf-8")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(min(length, 4096)).decode("utf-8"))
        except (ValueError, OSError):
            self._json({"ok": False})
            return
        a, b, winner = data.get("a"), data.get("b"), data.get("winner")
        pool = bt_model.load_pool()
        if a in pool and b in pool and a != b and winner in ("a", "b", "tie"):
            labels_path = self._labels_path()
            # cloudflare passes the real visitor IP; hash it so friends'
            # votes stay distinguishable without storing raw IPs
            who = "me" if labels_path == bt_model.LABELS_PATH else hashlib.md5(
                ((self.headers.get("CF-Connecting-IP") or self.client_address[0])
                 + (self.headers.get("User-Agent") or "")).encode()
            ).hexdigest()[:8]
            bt_model.append_label(a, b, winner, labels_path, who=who)
            self._json({"ok": True,
                        "labeled": len(bt_model.load_labels(labels_path))})
        else:
            self._json({"ok": False})

    def log_message(self, *args):
        pass


def ensure_singleton():
    """Kill the previous instance of this script via a PID file.

    Command-line matching is unsafe here: the host shell wraps commands in
    python processes whose command lines mirror ours — pattern matching once
    killed the wrapper (and ourselves with it). A PID file is exact.
    """
    import os
    import subprocess
    import tempfile
    pidfile = Path(tempfile.gettempdir()) / (Path(__file__).stem + ".pid")
    me = os.getpid()
    try:
        old = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        old = None
    if old and old != me:
        alive = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"$p = Get-Process -Id {old} -ErrorAction SilentlyContinue; if ($p) {{ 'yes' }} else {{ 'no' }}"],
            capture_output=True, text=True).stdout.strip()
        if alive == "yes":
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            f"Stop-Process -Id {old} -Force"], capture_output=True)
            print(f"[singleton] killed previous instance pid {old}", flush=True)
    try:
        pidfile.write_text(str(me))
    except OSError:
        pass


def main():
    ensure_singleton()
    print(f"douyin console: http://127.0.0.1:{PORT}/", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
