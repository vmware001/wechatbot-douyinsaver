"""Perceptual-hash dedup for downloaded douyin media.

Videos: sample N frames with OpenCV, compute a 64-bit difference hash
(dHash) per frame. Two videos are duplicates when they have the same frame
count and >=70% of frame pairs have Hamming distance <= 10.

Image posts: hash every downloaded image; duplicates when the image counts
match and every image pair is within the Hamming threshold.

Fingerprints persist in the manifest even after the files are deleted, so a
re-upload of an already-forwarded video is still caught.
"""
import queue  # stdlib preload
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

HASH_SIZE = 8          # 8x8 -> 64-bit hash
FRAME_SAMPLES = 6
HAMMING_MAX = 10       # of 64 bits
MATCH_RATIO = 0.7


def dhash(pil_img) -> int:
    img = pil_img.convert("L").resize((HASH_SIZE + 1, HASH_SIZE), Image.LANCZOS)
    a = np.asarray(img, dtype=np.uint8)
    diff = a[:, 1:] > a[:, :-1]
    v = 0
    for b in diff.flatten():
        v = (v << 1) | int(b)
    return v


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def video_fingerprint(path: Path, n_frames: int = FRAME_SAMPLES):
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return None
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    duration = total / fps if total else 0.0
    hashes = []
    for i in range(n_frames):
        if duration:
            pos_ms = duration * 1000.0 * (i + 0.5) / n_frames
            cap.set(cv2.CAP_PROP_POS_MSEC, pos_ms)
        ok, frame = cap.read()
        if not ok:
            break
        img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        hashes.append(dhash(img))
    cap.release()
    if not hashes:
        return None
    return {"kind": "video", "duration": round(duration, 2),
            "hashes": [format(h, "016x") for h in hashes]}


def images_fingerprint(paths):
    hashes = []
    for p in paths:
        try:
            with Image.open(p) as im:
                hashes.append(dhash(im))
        except Exception:
            return None
    return {"kind": "images", "duration": 0, "hashes": [format(h, "016x") for h in hashes]}


def fingerprint_files(files, kind):
    files = [Path(f) for f in files if Path(f).exists()]
    if not files:
        return None
    if kind == "images":
        return images_fingerprint(files)
    return video_fingerprint(files[0])


def _hx(fp):
    return [int(h, 16) for h in fp["hashes"]]


def is_dup(fp_a, fp_b) -> bool:
    if not fp_a or not fp_b or fp_a["kind"] != fp_b["kind"]:
        return False
    ha, hb = _hx(fp_a), _hx(fp_b)
    if not ha or len(ha) != len(hb):
        return False
    matches = sum(1 for a, b in zip(ha, hb) if hamming(a, b) <= HAMMING_MAX)
    return matches / len(ha) >= MATCH_RATIO


def find_dup(fp, manifest_data, exclude_id=None):
    """Return item_id of an existing duplicate, or None."""
    for iid, rec in manifest_data.get("videos", {}).items():
        if exclude_id is not None and iid == exclude_id:
            continue
        if is_dup(fp, rec.get("fp")):
            return iid
    return None


def stitch_images(paths, dest: Path, width=800, gap=6, quality=85, max_height=12000):
    """Vertically stitch a post's images into one jpg; returns dest or None."""
    imgs = []
    try:
        for p in paths:
            with Image.open(p) as im:
                im = im.convert("RGB")
                if im.width != width:
                    h = max(1, int(im.height * width / im.width))
                    im = im.resize((width, h), Image.LANCZOS)
                imgs.append(im)
        if not imgs:
            return None
        total_h = sum(im.height for im in imgs) + gap * (len(imgs) - 1)
        if total_h > max_height:  # keep WeChat-friendly dimensions
            scale = max_height / total_h
            w2 = max(1, int(width * scale))
            imgs = [im.resize((w2, max(1, int(im.height * scale))), Image.LANCZOS)
                    for im in imgs]
            width = w2
            total_h = sum(im.height for im in imgs) + gap * (len(imgs) - 1)
        canvas = Image.new("RGB", (width, total_h), (24, 24, 28))
        y = 0
        for im in imgs:
            canvas.paste(im, (0, y))
            y += im.height + gap
        dest.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(dest, "JPEG", quality=quality)
        return dest
    except Exception:
        return None
