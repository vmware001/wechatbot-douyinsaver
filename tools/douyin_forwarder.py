"""Forward saved Douyin media to a WeChat group as ONE compilation per burst.

Cycle: every `burst_interval_seconds` (default 2h):
  1. Collect all downloaded videos + image posts under watch_dir.
  2. Sort by curiosity ratio (share/digg) descending.
  3. Greedily pick segments until the total playtime would exceed
     `max_compilation_seconds` (default 300s). A video costs its real
     duration; each image costs `image_seconds` (default 2s).
  4. Normalize + concatenate everything into ONE mp4 (video_merge).
  5. Send it to the WeChat group (subject to quiet hours / chat hold).
  6. Only after a confirmed send: delete ALL local media (including items
     that were not selected or below threshold).

Console output is English-only to avoid codepage issues.
"""
import queue  # stdlib preload
import json
import logging
import math
import shutil
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import video_merge

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "douyin_forwarder.json"
WHITELIST_DIR = Path(tempfile.gettempdir()) / "WechatRobot"

log = logging.getLogger("douyin_forwarder")


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


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def manifest_index(manifest_path):
    """Map: resolved mp4 path -> record; resolved post folder -> record."""
    by_file, by_folder = {}, {}
    try:
        data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        for rec in data.get("videos", {}).values():
            files = rec.get("files") or []
            for fp in files:
                p = Path(fp)
                if p.suffix.lower() == ".mp4":
                    by_file[str(p.resolve())] = rec
            imgs = [Path(fp) for fp in files if fp.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
                    and Path(fp).name != "stitched.jpg"]
            if imgs:
                by_folder[str(imgs[0].parent.resolve())] = rec
    except Exception as e:
        log.warning("manifest unreadable: %s", e)
    return by_file, by_folder


def collect_items(watch_dir: Path, manifest_path, min_age, image_seconds,
                  max_video_seconds=0, min_ratio=0.0, min_digg=0,
                  min_video_seconds=0):
    """All media currently on disk, newest-last, with ratio/duration info.

    min_ratio/min_digg are the merge eligibility gate: the download threshold
    is deliberately wider than the merge threshold (extra videos feed the
    labeling pool, not the compilation), so items below the gate are skipped
    here even though they sit on disk."""
    now = time.time()
    by_file, by_folder = manifest_index(manifest_path)
    items = []
    for p in sorted(watch_dir.rglob("*.mp4"), key=lambda x: x.stat().st_mtime):
        if "image_debug" in p.parts:
            continue
        if now - p.stat().st_mtime < min_age:
            continue
        rec = by_file.get(str(p.resolve())) or {}
        ratio = float(rec.get("ratio") or 0.0)
        digg = int((rec.get("stats") or {}).get("digg", 0) or 0)
        if ratio < min_ratio or digg < min_digg:
            continue
        dur = (rec.get("duration_ms") or 0) / 1000.0
        if dur <= 0:
            try:
                dur, _ = video_merge.probe(p)
            except Exception as e:
                log.warning("probe failed for %s: %s", p.name, e)
                continue
        if min_video_seconds > 0 and 0 < dur < min_video_seconds:
            # the merge loops sub-min clips to whole repetitions; the budget
            # must price the looped length, not the raw one
            dur = math.ceil(min_video_seconds / dur) * dur
        if max_video_seconds > 0:
            dur = min(dur, max_video_seconds)
        items.append({"kind": "video", "path": p, "music": None,
                      "ratio": ratio,
                      "digg": digg,
                      "caption": rec.get("title") or rec.get("author") or "",
                      "cost": dur})
    for d in sorted([d for d in watch_dir.rglob("*")
                     if d.is_dir() and "image_debug" not in d.parts],
                    key=lambda x: x.stat().st_mtime):
        imgs = sorted(d.glob("img_*.*"))
        if not imgs:
            continue
        if now - max(i.stat().st_mtime for i in imgs) < min_age:
            continue
        rec = by_folder.get(str(d.resolve())) or {}
        ratio = float(rec.get("ratio") or 0.0)
        digg = int((rec.get("stats") or {}).get("digg", 0) or 0)
        if ratio < min_ratio or digg < min_digg:
            continue
        music = d / "music.mp3"
        cost = len(imgs) * image_seconds
        # 混合作品/动态照片的视频段也要计价（2s 下限循环、30s 上限截断，与拼接一致）
        for vfile in [*sorted(d.glob("live_*.mp4")), d / "video.mp4"]:
            if not vfile.exists():
                continue
            try:
                vdur, _ = video_merge.probe(vfile)
            except Exception:
                vdur = 0.0
            if vdur > 0:
                if min_video_seconds > 0 and vdur < min_video_seconds:
                    vdur = math.ceil(min_video_seconds / vdur) * vdur
                cost += min(30.0, vdur)
        # 单个图文作品封顶 30s（超出截断，与 video_merge 一致）
        cost = min(30.0, cost)
        items.append({"kind": "images", "path": d,
                      "music": music if music.exists() else None,
                      "ratio": ratio,
                      "digg": digg,
                      "caption": rec.get("title") or rec.get("author") or "",
                      "cost": cost})
    return items


def select_budget(items, budget_seconds):
    """Greedy pick by curiosity desc while total cost fits the budget.
    Items that alone exceed the budget are skipped (they can never fit)."""
    ranked = sorted(items, key=lambda x: -x["ratio"])
    picked, total = [], 0.0
    for it in ranked:
        if total + it["cost"] <= budget_seconds:
            picked.append(it)
            total += it["cost"]
    return picked, total


def send_video(sender_url, token, target, file_path: Path):
    body = json.dumps({"target": target, "file": str(file_path)}).encode("utf-8")
    req = urllib.request.Request(
        sender_url, data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Token": token})
    with urllib.request.urlopen(req, timeout=600) as r:
        return r.status, r.read().decode("utf-8", "replace")[:300]


def safe_staging_name(name: str) -> str:
    """Filenames with symbols outside GBK (e.g. '✔') make the sender's
    clipboard copy fail with EINVAL — strip anything non-GBK-encodable."""
    try:
        name.encode("gbk")
        return name
    except UnicodeEncodeError:
        cleaned = name.encode("gbk", "replace").decode("gbk").replace("?", "")
        return cleaned or "video.mp4"


def send_one(cfg, target, src: Path, attempts: int = 5) -> bool:
    staging = WHITELIST_DIR / safe_staging_name(src.name)
    for attempt in range(1, attempts + 1):
        shutil.copy2(src, staging)
        log.info("sending %s (%.1f MB) -> %s (attempt %d/%d)",
                 src.name, src.stat().st_size / 1e6, target, attempt, attempts)
        try:
            status, text = send_video(cfg["sender_url"], cfg["token"], target, staging)
        except Exception as e:
            log.error("send request failed: %s", e)
            status, text = None, str(e)
        finally:
            staging.unlink(missing_ok=True)
        if status == 200:
            return True
        log.error("send failed (%s): %s", status, text)
        time.sleep(30)
    return False


def record_forwarded(watch_dir: Path, count: int, seconds: float, group: str = ""):
    """Append one JSON line per successful burst for /about stats."""
    path = watch_dir / "forwarded_history.jsonl"
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "time": datetime.now().isoformat(timespec="seconds"),
                "count": count,
                "seconds": round(seconds, 1),
                "group": group,
            }, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("history append failed: %s", e)


def delete_all_media(watch_dir: Path):
    """After a confirmed send, wipe every mp4 and image post folder
    (selected or not). stats.csv files stay. Empty day folders go too.
    image_debug holds tooling screenshots/test clips — never touched."""
    n = 0
    for p in watch_dir.rglob("*.mp4"):
        if "image_debug" in p.parts:
            continue
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    for d in sorted([d for d in watch_dir.rglob("*")
                     if d.is_dir() and "image_debug" not in d.parts],
                    key=lambda x: -len(x.parts)):
        if d == watch_dir:
            continue
        if list(d.glob("img_*.*")) or not any(d.iterdir()):
            try:
                shutil.rmtree(d)
                n += 1
            except OSError:
                pass
    log.info("cleanup: removed %d media files/folders", n)


def in_quiet_hours(now: datetime, start_s: str, end_s: str) -> bool:
    t = now.strftime("%H:%M")
    if start_s <= end_s:
        return start_s <= t < end_s
    return t >= start_s or t < end_s  # overnight window, e.g. 23:00-09:00


class GroupActivity:
    """Realtime activity counter backed by the bot's group memory file
    (data/groups/<group>/group_messages.json). Each entry carries the real
    arrival timestamp written by the WeChat frontend, so a lazy poll after a
    long burst sleep cannot mistake hours of backlog for fresh chatter
    (the old run.log tailer did exactly that and held sends for no reason)."""

    def __init__(self, messages_file, window_seconds):
        self.path = Path(messages_file)
        self.window = window_seconds

    def poll(self) -> int:
        now = time.time()
        try:
            with open(self.path, encoding="utf-8") as f:
                messages = json.load(f)
        except (OSError, ValueError):
            return 0
        n = 0
        for item in messages:
            if not isinstance(item, dict) or item.get("is_bot"):
                continue
            try:
                ts = float(item.get("timestamp") or 0)
            except (TypeError, ValueError):
                continue
            if ts >= now - self.window:
                n += 1
        return n


def seconds_until_quiet_end(now: datetime, end_s: str) -> float:
    h, m = map(int, end_s.split(":"))
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def wait_for_send_window(cfg, activity, quiet_start, quiet_end):
    """Block until quiet hours and chat-hold both allow sending."""
    while True:
        if quiet_start and quiet_end and in_quiet_hours(datetime.now(), quiet_start, quiet_end):
            wait = seconds_until_quiet_end(datetime.now(), quiet_end)
            log.info("quiet hours %s-%s, holding sends for %.0f min",
                     quiet_start, quiet_end, wait / 60)
            time.sleep(min(wait, 3600))
            continue
        if activity is not None:
            n = activity.poll()
            if n >= int(cfg.get("chat_hold", {}).get("min_messages", 5)):
                log.info("group is chatting (%d msgs), holding sends", n)
                time.sleep(45)
                continue
        return


def main():
    ensure_singleton()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    cfg = load_config()
    target = cfg["target"]
    allowed = set(cfg.get("allowed_targets", ["Your Target Group", "root"]))
    if target not in allowed:
        raise SystemExit(f"target must be one of {sorted(allowed)}, got {target!r}")
    watch_dir = Path(cfg["watch_dir"])
    burst_interval = float(cfg["burst_interval_seconds"])
    budget = float(cfg.get("max_compilation_seconds", 300))
    image_seconds = float(cfg.get("image_seconds", 2))
    max_segment = float(cfg.get("max_segment_seconds", 20))
    # clips shorter than this are looped (whole repetitions) inside the
    # compilation instead of flashing by
    min_segment = float(cfg.get("min_segment_seconds", 2))
    min_age = float(cfg.get("min_file_age_seconds", 60))
    # merge gate: separate from the (deliberately wider) download threshold,
    # so extra downloads feed the labeling pool without diluting compilations
    merge_min_ratio = float(cfg.get("merge_min_ratio", 2.0))
    merge_min_digg = int(cfg.get("merge_min_digg", 5))
    manifest_path = str(Path(cfg["watch_dir"]) / "manifest.json")
    quiet = cfg.get("quiet_hours") or {}
    quiet_start = str(quiet.get("start", ""))
    quiet_end = str(quiet.get("end", ""))
    chat_hold = cfg.get("chat_hold") or {}
    activity = None
    if chat_hold.get("enabled"):
        messages_file = chat_hold.get("messages_file") or str(
            BASE_DIR / "data" / "groups" / target / "group_messages.json")
        activity = GroupActivity(messages_file,
                                 int(chat_hold.get("window_minutes", 10)) * 60)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(watch_dir / "douyin_forwarder.log",
                                      encoding="utf-8")])
    WHITELIST_DIR.mkdir(parents=True, exist_ok=True)
    build_dir = Path(tempfile.gettempdir()) / "douyin_compilation"
    build_dir.mkdir(parents=True, exist_ok=True)
    # stray staging leftovers (e.g. from a killed process) would otherwise
    # accumulate in the temp dir — clean mp4s older than a day at startup.
    for stray in WHITELIST_DIR.glob("*.mp4"):
        try:
            if time.time() - stray.stat().st_mtime > 86400:
                stray.unlink()
                log.info("removed stray staging file %s", stray.name)
        except OSError:
            pass

    log.info("forwarder started, target=%r burst=%ss budget=%ss",
             target, burst_interval, budget)
    while True:
        burst_start = time.time()
        try:
            items = collect_items(watch_dir, manifest_path, min_age, image_seconds,
                                  max_segment, merge_min_ratio, merge_min_digg,
                                  min_segment)
            if not items:
                log.info("queue empty, waiting for next burst")
            else:
                log.info("%d item(s) on disk, waiting for send window", len(items))
                wait_for_send_window(cfg, activity, quiet_start, quiet_end)
                # collect + rank AFTER the gate, right before merging, so
                # anything saved while the gate was closed (quiet hours,
                # chat hold) joins this burst and a new hottest video
                # takes the front of the compilation
                items = collect_items(watch_dir, manifest_path, min_age, image_seconds,
                                      max_segment, merge_min_ratio, merge_min_digg,
                                      min_segment)
                picked, total = select_budget(items, budget)
                log.info("burst: %d item(s) on disk, picked %d (%.0fs of %.0fs budget)",
                         len(items), len(picked), total, budget)
                if picked:
                    # picked keeps select_budget's curiosity-descending order,
                    # which is also the playback order of the compilation
                    log.info("burst order (ratio desc): %s",
                             ", ".join("%.1f" % it["ratio"] for it in picked))
            if items and not picked:
                log.info("queue empty after re-scan, waiting for next burst")
            elif items:
                merged = build_dir / ("compilation_"
                                      + datetime.now().strftime("%Y%m%d_%H%M") + ".mp4")
                merged.unlink(missing_ok=True)
                compiled = video_merge.build_compilation(picked, merged, image_seconds,
                                                         max_segment, min_segment)
                log.info("compilation built: %.0fs, %.1f MB",
                         compiled, merged.stat().st_size / 1e6)
                if send_one(cfg, target, merged):
                    log.info("sent and deleted %s", merged.name)
                    record_forwarded(watch_dir, len(picked), compiled, group=target)
                    delete_all_media(watch_dir)
                    merged.unlink(missing_ok=True)
                else:
                    log.warning("send failed; media kept for next burst")
            wait = burst_start + burst_interval - time.time()
            if wait > 0:
                log.info("burst window done, next burst in %.0fs", wait)
                time.sleep(wait)
        except KeyboardInterrupt:
            log.info("stopped by user")
            break
        except Exception as e:
            log.error("loop error: %s", e)
            time.sleep(60)


if __name__ == "__main__":
    main()
