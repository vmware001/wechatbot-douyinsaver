"""One-off: copy currently on-disk downloaded items into the label pool.

pool_add() only fires at download time, so items downloaded before image
posts were pool-eligible (or before the pool existed) never got copied.
Anything still on disk right now (not yet wiped by a burst) gets pooled.
"""
import json
import shutil
from pathlib import Path

BASE = Path(r"C:\GSMPSWechatBot")
POOL = BASE / "data" / "label_pool"
POOL.mkdir(parents=True, exist_ok=True)
manifest = json.loads((BASE / "data" / "douyin_saved" / "manifest.json")
                      .read_text(encoding="utf-8"))

added = 0
for iid, rec in (manifest.get("videos") or {}).items():
    if not rec.get("downloaded"):
        continue
    files = [Path(f) for f in rec.get("files") or []]
    files = [f for f in files if f.exists()]
    if not files:
        continue
    if rec.get("type") == "images":
        stitched = [f for f in files if f.name == "stitched.jpg"]
        if not stitched:
            continue
        dest = POOL / f"{iid}.jpg"
        src = stitched[0]
    else:
        mp4 = [f for f in files if f.suffix == ".mp4"]
        if not mp4:
            continue
        dest = POOL / f"{iid}.mp4"
        src = mp4[0]
    if dest.exists():
        continue
    shutil.copy2(src, dest)
    added += 1

print(f"added {added} items to pool; pool now {len(list(POOL.glob('*')))} files")
