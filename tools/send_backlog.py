# -*- coding: utf-8 -*-
"""Backlog sender: push saved Douyin media to the WeChat group right now.

Why this exists: the forwarder (tools/douyin_forwarder.py) sends one
compilation per burst and is gated by quiet hours; when the send path is down
(e.g. the WeChat sender service was broken), media piles up and only trickles
out after the next open window. This script replays the same pipeline
immediately:

    collect -> select_budget -> build_compilation -> send_one

Differences from the normal burst loop, on purpose:
  * quiet hours are honored unless --force is given (chat hold is not checked,
    this is a manual catch-up tool);
  * after a successful send only the media that went into the compilation is
    deleted — whatever did not fit the 300s budget stays queued for the next
    round instead of being wiped.

Console output is English-only to avoid codepage issues.

Usage:
    python tools/send_backlog.py            # send until the pool is empty (max 6 rounds)
    python tools/send_backlog.py --rounds 2 # stop after 2 compilations
    python tools/send_backlog.py --dry-run  # show what would be sent, send nothing
    python tools/send_backlog.py --force    # also send during quiet hours (outage only)
"""

import argparse
import logging
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import video_merge  # noqa: E402
from douyin_forwarder import (  # noqa: E402
    collect_items, in_quiet_hours, load_config, record_forwarded, select_budget,
    send_one,
)

log = logging.getLogger("send_backlog")


def delete_picked(watch_dir: Path, picked, protect_dirs=()):
    """Remove only the media that went into the compilation.

    NEVER touches the labeling pool: media under any `protect_dirs` entry
    (data/label_pool — the label UI serves from there) is skipped even if a
    path somehow pointed there. Only files inside watch_dir are eligible.
    """
    watch_root = Path(watch_dir).resolve()
    protected = [Path(p).resolve() for p in protect_dirs]
    n = 0
    for it in picked:
        p = Path(it["path"]).resolve()
        try:
            if not p.is_relative_to(watch_root):
                log.warning("skip delete outside watch dir: %s", p)
                continue
            if any(p == d or d in p.parents for d in protected):
                log.warning("skip delete (labeling pool): %s", p)
                continue
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
                n += 1
            elif p.exists():
                p.unlink()
                n += 1
        except OSError:
            pass
    log.info("cleanup: removed %d sent media item(s)", n)


def run(rounds: int, dry_run: bool, force: bool = False) -> int:
    cfg = load_config()
    target = cfg["target"]
    watch_dir = Path(cfg["watch_dir"])
    manifest_path = str(watch_dir / "manifest.json")
    # 夜间免打扰与转发器同一套配置（quiet_hours）；默认遵守，
    # 只有显式 --force 才例外（例如前端故障后的补发）。
    quiet = cfg.get("quiet_hours") or {}
    qs, qe = str(quiet.get("start", "")), str(quiet.get("end", ""))
    if qs and qe and not force and in_quiet_hours(datetime.now(), qs, qe):
        log.info("quiet hours %s-%s: refusing to send now; rerun with "
                 "--force to override (e.g. after an outage)", qs, qe)
        return 0
    # 标注池永远不删（人工标注要用）：池路径记在 saver 的配置里（label_pool_dir），
    # 读不到就用默认 data/label_pool
    label_pool_dir = watch_dir.parent / "label_pool"
    try:
        import json as _json
        _sc = _json.loads((Path(__file__).resolve().parent.parent
                           / "config" / "douyin_saver.json").read_text(encoding="utf-8"))
        if _sc.get("label_pool_dir"):
            label_pool_dir = Path(_sc["label_pool_dir"])
    except Exception:
        pass
    budget = float(cfg.get("max_compilation_seconds", 300))
    image_seconds = float(cfg.get("image_seconds", 2))
    max_segment = float(cfg.get("max_segment_seconds", 20))
    min_segment = float(cfg.get("min_segment_seconds", 2))
    min_age = float(cfg.get("min_file_age_seconds", 60))
    merge_min_ratio = float(cfg.get("merge_min_ratio", 2.0))
    merge_min_digg = int(cfg.get("merge_min_digg", 5))
    build_dir = Path(tempfile.gettempdir()) / "douyin_compilation"
    build_dir.mkdir(parents=True, exist_ok=True)

    sent_rounds = 0
    for rnd in range(1, rounds + 1):
        items = collect_items(watch_dir, manifest_path, min_age, image_seconds,
                              max_segment, merge_min_ratio, merge_min_digg,
                              min_segment)
        if not items:
            log.info("round %d: pool is empty, nothing to send", rnd)
            break
        picked, total = select_budget(items, budget)
        log.info("round %d: %d item(s) on disk, picked %d (%.0fs of %.0fs budget)",
                 rnd, len(items), len(picked), total, budget)
        if not picked:
            log.info("round %d: nothing fits the budget, stopping", rnd)
            break
        log.info("round %d order (ratio desc): %s", rnd,
                 ", ".join("%.1f" % it["ratio"] for it in picked))
        if dry_run:
            for it in picked:
                log.info("would send: %s (ratio %.1f, cost %.0fs)",
                         it["path"].name if it["kind"] == "video" else it["path"].name,
                         it["ratio"], it["cost"])
            break

        merged = build_dir / ("backlog_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".mp4")
        merged.unlink(missing_ok=True)
        skipped = []
        try:
            compiled = video_merge.build_compilation(picked, merged, image_seconds,
                                                     max_segment, min_segment,
                                                     skipped=skipped)
        except RuntimeError as e:
            log.error("round %d: nothing could be compiled (%s) - stopping", rnd, e)
            return 1
        if skipped:
            log.warning("round %d: %d item(s) could not be compiled and are "
                        "kept on disk (not sent, not deleted)", rnd, len(skipped))
        sent = [it for it in picked if Path(it["path"]).resolve()
                not in {Path(p).resolve() for p in skipped}]
        log.info("round %d: compilation built %.0fs, %.1f MB", rnd,
                 compiled, merged.stat().st_size / 1e6)
        ok = send_one(cfg, target, merged)
        merged.unlink(missing_ok=True)
        if not ok:
            log.error("round %d: send failed, media kept on disk - aborting", rnd)
            return 1
        record_forwarded(watch_dir, len(sent), compiled, group=target)
        delete_picked(watch_dir, sent, protect_dirs=(label_pool_dir,))
        sent_rounds += 1
        if len(items) - len(picked) <= 0:
            break
        time.sleep(3)  # gap between compilations, avoid hammering the sender

    log.info("done: %d compilation(s) sent", sent_rounds)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=6,
                    help="max compilations to send in this run (default 6)")
    ap.add_argument("--dry-run", action="store_true", help="list what would be sent")
    ap.add_argument("--force", action="store_true",
                    help="send even during quiet hours (outage catch-up only)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(
                      Path(__file__).resolve().parent.parent
                      / "data" / "douyin_saved" / "send_backlog.log",
                      encoding="utf-8")])
    return run(max(1, args.rounds), args.dry_run, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
