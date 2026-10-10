"""One-off: convert pooled stitched .jpg image posts into 2s-per-image videos
when the original image folder is still on disk (bursts wipe them)."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import video_merge

BASE = Path(r"C:\GSMPSWechatBot")
POOL = BASE / "data" / "label_pool"
manifest = json.loads((BASE / "data" / "douyin_saved" / "manifest.json")
                      .read_text(encoding="utf-8"))
records = manifest.get("videos") or {}

converted = kept = 0
for jpg in list(POOL.glob("*.jpg")):
    iid = jpg.stem
    rec = records.get(iid) or {}
    files = [Path(f) for f in rec.get("files") or []]
    folder = files[0].parent if files else None
    if folder is None or not list(folder.glob("img_*.*")):
        kept += 1
        continue
    music = folder / "music.mp3"
    tmp = Path(tempfile.mkdtemp(prefix="pool_conv_")) / f"{iid}.mp4"
    try:
        video_merge.build_compilation(
            [{"kind": "images", "path": folder,
              "music": music if music.exists() else None}],
            tmp, image_seconds=2.0)
        tmp.replace(POOL / f"{iid}.mp4")
        jpg.unlink()
        converted += 1
        print(f"converted {iid}")
    except Exception as e:
        print(f"FAILED {iid}: {e}")
        kept += 1

print(f"converted={converted} kept_jpg={kept}")
