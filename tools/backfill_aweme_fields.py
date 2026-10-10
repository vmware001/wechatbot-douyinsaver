"""Backfill create_time + is_aigc_media into the saver manifest.

The two fields were added to the manifest record after the first pool items
were already saved. The raw mitm captures (api_*.jsonl) still hold the full
aweme payloads, so the fields can be recovered without any new API calls.

Usage: .venv/Scripts/python.exe tools/backfill_aweme_fields.py
Idempotent: records that already have both fields are skipped.
"""

import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
MITM_DIR = BASE_DIR / "data" / "douyin_mitm"
MANIFEST = BASE_DIR / "data" / "douyin_saved" / "manifest.json"


def scan_mitm():
    """aweme_id -> (create_time, is_aigc_media) from raw captures."""
    found = {}
    for path in sorted(MITM_DIR.glob("api_*.jsonl")):
        with open(path, "rb") as f:
            for raw in f:
                if b"aweme_detail" not in raw:
                    continue
                try:
                    rec = json.loads(raw.decode("utf-8", "replace"))
                    payload = json.loads(rec["body"])
                except ValueError:
                    continue
                items = []
                if isinstance(payload.get("aweme_detail"), dict):
                    items.append(payload["aweme_detail"])
                for it in payload.get("aweme_details") or []:
                    if isinstance(it, dict):
                        items.append(it)
                for a in items:
                    iid = str(a.get("aweme_id") or "")
                    if iid:
                        found[iid] = (int(a.get("create_time") or 0),
                                      1 if a.get("is_aigc_media") else 0)
    return found


def main():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    videos = manifest.get("videos") or {}
    found = scan_mitm()
    print(f"mitm index: {len(found)} items, manifest: {len(videos)} records")

    updated = missing = already = 0
    for iid, rec in videos.items():
        if rec.get("create_time"):
            already += 1
            continue
        hit = found.get(iid)
        if not hit:
            missing += 1
            continue
        rec["create_time"], rec["is_aigc_media"] = hit
        updated += 1

    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False),
                        encoding="utf-8")
    print(f"updated: {updated}, already had fields: {already}, "
          f"not in mitm captures: {missing}")


if __name__ == "__main__":
    sys.exit(main())
