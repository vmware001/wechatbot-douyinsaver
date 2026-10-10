# -*- coding: utf-8 -*-
"""把已标注但媒体被池子容量淘汰的视频重新拉回标注池。

2026-10-03 事故：标注池 400 容量满了按文件时间淘汰最旧，被标注的正是最旧那批，
216 个已标注视频的媒体被清掉（labels.jsonl 的投票完好，模型不受影响，但没法回看）。
本脚本按 labels*.jsonl 的 a/b id 对照 manifest，缺媒体的逐个经浏览器取新鲜地址
重新下载入池。已标注条目现在受淘汰保护（见 douyin_saver._labeled_ids），不会再被删。

用法：
    python tools/recover_label_media.py            # 恢复全部缺失的
    python tools/recover_label_media.py --limit 5  # 先试 5 个
"""

import argparse
import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "tools"))

import bt_model  # noqa: E402
from douyin_saver import (  # noqa: E402
    Browser, CDP, download, is_audio_url, normalize_aweme, pool_add, pool_add_images,
)

log = logging.getLogger("recover_label_media")


def labeled_ids(pool_dir: Path) -> set:
    ids = set()
    for lf in pool_dir.glob("labels*.jsonl"):
        try:
            for line in lf.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                for k in ("a", "b"):
                    v = rec.get(k)
                    if v:
                        ids.add(str(v))
        except Exception:
            continue
    return ids


def recover_one(browser, cfg, iid: str, rec: dict) -> bool:
    """下载一个作品的媒体并入池；成功返回 True。"""
    d = browser.fetch_detail(iid)
    if not d.get("ok"):
        log.warning("fetch failed for %s", iid)
        return False
    imgs = d.get("images") or []
    lives = d.get("image_videos") or []
    vurls = [a for a in (d.get("play_addrs") or []) if not is_audio_url(a)]
    murls = d.get("music") or []
    caption = (rec.get("title") or rec.get("author") or "")

    if imgs:
        folder = Path(tempfile.mkdtemp(prefix="recover_")) / iid
        folder.mkdir(parents=True)
        try:
            for i, u in enumerate(imgs, 1):
                ext = ".jpg"
                import re as _re
                m = _re.search(r"\.(jpg|jpeg|png|webp)", u.split("?")[0], _re.I)
                if m:
                    ext = "." + m.group(1).lower().replace("jpeg", "jpg")
                try:
                    download(u, folder / f"img_{i:02d}{ext}", min_bytes=1024)
                except Exception as e:
                    log.warning("img %d download failed for %s: %s", i, iid, e)
                if i - 1 < len(lives) and lives[i - 1]:
                    try:
                        download(lives[i - 1], folder / f"live_{i:02d}.mp4", min_bytes=20480)
                    except Exception as e:
                        log.warning("live %d download failed for %s: %s", i, iid, e)
            if vurls:
                try:
                    download(vurls[0], folder / "video.mp4", min_bytes=20480)
                except Exception as e:
                    log.warning("mixed video download failed for %s: %s", iid, e)
            music = folder / "music.mp3"
            if murls:
                try:
                    download(murls[0], music, timeout=30, min_bytes=1024)
                except Exception:
                    music = None
            return bool(pool_add_images(cfg, iid, folder,
                                        music if music and music.exists() else None,
                                        caption=caption))
        finally:
            shutil.rmtree(folder.parent, ignore_errors=True)

    if vurls:
        tmp = Path(tempfile.mkdtemp(prefix="recover_v_")) / f"{iid}.mp4"
        try:
            download(vurls[0], tmp, min_bytes=20480)
            return bool(pool_add(cfg, iid, tmp))
        except Exception as e:
            log.warning("video download failed for %s: %s", iid, e)
            return False
        finally:
            shutil.rmtree(tmp.parent, ignore_errors=True)

    log.warning("no downloadable media for %s", iid)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只恢复前 N 个（0=全部）")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = json.loads((BASE_DIR / "config" / "douyin_saver.json").read_text(encoding="utf-8"))
    pool_dir = Path(cfg.get("label_pool_dir") or (BASE_DIR / "data" / "label_pool"))

    labels = bt_model.load_labels()
    pool = bt_model.load_pool()
    need = labeled_ids(pool_dir)
    missing = [i for i in sorted(need) if i not in pool]
    if args.limit:
        missing = missing[:args.limit]
    log.info("labeled=%d pool=%d missing=%d -> recovering", len(need), len(pool), len(missing))
    if not missing:
        log.info("nothing to recover")
        return 0

    # manifest 记录（title/author 拿来做简介）
    try:
        manifest = json.loads((bt_model.MANIFEST_PATH).read_text(encoding="utf-8"))
        records = manifest.get("videos") or {}
    except Exception:
        records = {}

    browser = Browser("http://127.0.0.1:9223")
    browser.api = CDP(browser.debug_url, browser._chat_pages()[0])

    ok = fail = 0
    for n, iid in enumerate(missing, 1):
        rec = records.get(iid) or {}
        try:
            if recover_one(browser, cfg, iid, rec):
                ok += 1
            else:
                fail += 1
        except Exception as e:
            fail += 1
            log.warning("recover %s failed: %s", iid, e)
        if n % 20 == 0:
            log.info("progress %d/%d ok=%d fail=%d", n, len(missing), ok, fail)
    log.info("done: recovered=%d failed=%d", ok, fail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
