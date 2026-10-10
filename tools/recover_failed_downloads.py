"""One-off recovery: re-download items whose downloads failed during the
system-proxy cert window (19:31-19:52), using passively captured detail
payloads (zero api calls). Also serves as a MitmFeed smoke test.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import douyin_saver as ds

ROOT = Path(r"C:\GSMPSWechatBot\data\douyin_saved")
manifest_path = ROOT / "manifest.json"
data = json.loads(manifest_path.read_text(encoding="utf-8"))

feed = ds.MitmFeed(r"C:\GSMPSWechatBot\data\douyin_mitm")
feed.poll()
print(f"mitm feed index: {len(feed.index)} items")

cfg = ds.load_config()
threshold = float(cfg.get("download_ratio_threshold", 6.0))
min_digg = int(cfg.get("download_min_digg", 10))
today = time.strftime("%Y-%m-%d")

recovered, nofeed, skipped = 0, 0, 0
for iid, rec in list(data["videos"].items()):
    if rec.get("downloaded") or rec.get("files"):
        continue
    stats = rec.get("stats") or {}
    digg = int(stats.get("digg", 0) or 0)
    if digg < min_digg or float(rec.get("ratio", 0) or 0) < threshold:
        continue
    if not str(rec.get("saved_at", "")).startswith(today):
        continue
    detail = feed.lookup(iid)
    if not detail:
        nofeed += 1
        print(f"  no passive payload for {iid} ({rec.get('author')})")
        continue
    kind = rec.get("type", "video")
    day = time.strftime("%Y%m%d")
    folder = ROOT / ds.sanitize(cfg["group_name"]) / day
    saved = []
    if kind == "images":
        sub = folder / f"recover_{ds.sanitize(rec.get('author',''))}_{iid[-8:]}"
        for i, u in enumerate(detail.get("images") or [], 1):
            dest = sub / f"img_{i:02d}.jpg"
            try:
                ds.download(u, dest)
                saved.append(str(dest))
            except Exception as e:
                print(f"  image download failed {iid}: {e}")
        music = detail.get("music") or []
        if music and saved:
            try:
                ds.download(music[0], sub / "music.mp3", timeout=30)
            except Exception as e:
                print(f"  music download failed {iid}: {e}")
    else:
        url = ds.pick_play_addr(detail.get("play_addrs") or [])
        if url:
            dest = folder / f"recover_{ds.sanitize(rec.get('author',''))}_{iid[-8:]}.mp4"
            try:
                ds.download(url, dest)
                saved.append(str(dest))
            except Exception as e:
                print(f"  video download failed {iid}: {e}")
    if saved:
        rec["files"] = saved
        rec["downloaded"] = True
        if not rec.get("duration_ms"):
            rec["duration_ms"] = int(detail.get("duration") or 0)
        recovered += 1
        print(f"  RECOVERED {iid} [{rec.get('author')}] {len(saved)} file(s)")
    else:
        skipped += 1

if recovered:
    manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                             encoding="utf-8")
print(f"done: recovered={recovered} nofeed={nofeed} skipped={skipped}")
