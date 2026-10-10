"""One-time dedup pass over the existing download folder + manifest.

- Backfills perceptual fingerprints for records whose files still exist.
- Detects duplicates (against earlier-saved records, including ones whose
  files were already forwarded/deleted) and removes the duplicate files.

Stop the saver and forwarder before running this.
"""
import queue  # stdlib preload
import json
from pathlib import Path

import media_dedup

MANIFEST = Path(r"C:\GSMPSWechatBot\data\douyin_saved\manifest.json")


def main():
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    videos = data.get("videos", {})

    # 1) backfill fingerprints
    n_fp = 0
    for iid, rec in videos.items():
        files = [f for f in rec.get("files", []) if Path(f).exists()]
        if files and not rec.get("fp"):
            fp = media_dedup.fingerprint_files(files, rec.get("type", "video"))
            if fp:
                rec["fp"] = fp
                n_fp += 1
    print(f"fingerprints computed: {n_fp}")

    # 2) dedup, oldest record wins
    seen = []
    n_dup = 0
    for iid, rec in sorted(videos.items(), key=lambda kv: kv[1].get("saved_at", "")):
        fp = rec.get("fp")
        if not fp:
            continue
        match = next((sid for sid, sfp in seen if media_dedup.is_dup(fp, sfp)), None)
        if match:
            for f in rec.get("files", []):
                p = Path(f)
                if p.exists():
                    p.unlink()
                    print(f"  deleted dup file: {p.name}")
            rec["files"] = []
            rec["downloaded"] = False
            rec["dup_of"] = match
            n_dup += 1
            print(f"DUP {iid} == {match}")
        else:
            seen.append((iid, fp))
    print(f"duplicates removed: {n_dup}")

    MANIFEST.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print("manifest saved")


if __name__ == "__main__":
    main()
