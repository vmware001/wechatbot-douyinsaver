"""Merge douyin videos / image posts into one compilation mp4.

Strategy: normalize every segment to an identical intermediate
(720x1280, 30fps, h264 + aac 44.1kHz stereo, silence injected when the
source has no audio), then concatenate with the concat demuxer (-c copy).
This survives mixed resolutions, orientations and missing audio tracks.

ffmpeg comes from imageio-ffmpeg (no system install needed).
Console output is English-only.
"""
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

log = logging.getLogger("video_merge")

VF = ("scale=720:1280:force_original_aspect_ratio=decrease,"
      "pad=720:1280:(ow-iw)/2:(oh-ih)/2:black,fps=30,setsar=1")
SILENCE = "anullsrc=channel_layout=stereo:sample_rate=44100"
DURATION_RE = re.compile(r"Duration: (\d+):(\d+):([\d.]+)")
FONT_FILE = r"C\:/Windows/Fonts/msyh.ttc"
# 单个图文作品（多图/动态照片/夹带视频）合成的总时长上限（2026-10-02 用户规则：
# 图很多很多的作品也不能超 30s，超出直接截断）
MAX_ITEM_SECONDS = 30.0
# 字幕折行预算（2026-10-01 实测，fontsize=24 + 2px 描边）：
# 汉字 23.9px、大写 16.8、小写 14.6、数字 13.8、混合英文均 12.2 ——
# 中英文宽度比 ≈ 2:1。720 宽画面两边各留 16px ≈ 688px 可用，按 670px 折行。
# 行距实测：line_spacing=-12 时两行总高 61px（间隙约 5px）；正数/0 间隙过大。
CAPTION_LINE_PX = 670
CAPTION_MAX_LINES = 2
CAPTION_LINE_SPACING = -12


def _caption_char_px(ch: str) -> int:
    """单字符像素宽（实测值，用于折行）。"""
    if ord(ch) > 0x2E80:
        return 24          # 汉字 / 全角标点
    if ch == " ":
        return 7
    if ch.isdigit():
        return 14
    if ch.isupper():
        return 17
    if ch.islower():
        return 15
    return 10              # 其余 ASCII 标点


def _caption_wrap(text: str) -> str:
    """把简介折成不超过两行（drawtext 的 textfile 支持多行）。"""
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return ""
    lines, cur, cur_w = [], "", 0
    for ch in t:
        w = _caption_char_px(ch)
        if cur_w + w > CAPTION_LINE_PX:
            lines.append(cur)
            cur, cur_w = "", 0
            if len(lines) == CAPTION_MAX_LINES:
                lines[-1] = lines[-1][:-1] + "…" if lines[-1] else "…"
                return "\n".join(lines)
        cur += ch
        cur_w += w
    if cur:
        lines.append(cur)
    return "\n".join(lines[:CAPTION_MAX_LINES])


def _caption_filter(caption, cap_path) -> str:
    """把作品简介画在片段左上角（24px 小字、描边、两行封顶、窄行距）。

    2026-10-01 用户要求：长视频里每个视频带简介，字体不要太大、不超过两行、
    别挡住视频 —— 放左上角。多行文字用 textfile（text= 里塞换行会被滤镜解析器吃掉）。
    """
    t = _caption_wrap(caption)
    if not t:
        return ""
    p = Path(str(cap_path))
    try:
        p.write_text(t, encoding="utf-8")
    except OSError as e:
        log.warning("caption file write failed: %s", e)
        return ""
    return (",drawtext=fontfile='" + FONT_FILE + "':textfile='" +
            str(p).replace("\\", "/").replace(":", r"\:") +
            "':fontsize=24:fontcolor=white:borderw=2:bordercolor=black@0.8"
            ":x=16:y=12:line_spacing=" + str(CAPTION_LINE_SPACING))


def _run(args, timeout=600):
    return subprocess.run([FFMPEG, *args], capture_output=True, timeout=timeout)


_COLOR_BSF = ("h264_metadata=colour_primaries=1:transfer_characteristics=1:"
              "matrix_coefficients=1:video_full_range_flag=0")


def _sanitize_color(src: Path):
    """Rewrite bogus H.264 colour metadata without re-encoding.

    Some douyin clips carry invalid VUI colour values
    (probed as 'reserved/reserved/smpte170m'); ffmpeg 7.x then refuses every
    encode of that stream with 'Invalid color space' — even with no filters.
    The h264_metadata bitstream filter rewrites the SPS VUI to bt709 while
    copying the bitstream (fast, no quality loss). Returns a temp mp4 path,
    or None if the clean did not help. Caller deletes the temp file.
    """
    try:
        fd, name = tempfile.mkstemp(suffix=".mp4", prefix="douyin_colorfix_")
        os.close(fd)
        dst = Path(name)
        r = _run(["-y", "-i", str(src), "-bsf:v", _COLOR_BSF,
                  "-c", "copy", str(dst)], timeout=300)
        if r.returncode == 0 and dst.exists() and dst.stat().st_size > 0:
            return dst
        dst.unlink(missing_ok=True)
    except Exception as e:
        log.warning("color sanitize failed for %s: %s", src, e)
    return None


def probe(path: Path):
    """Return (duration_seconds, has_audio)."""
    r = _run(["-i", str(path)], timeout=60)
    text = r.stderr.decode("utf-8", "replace")
    m = DURATION_RE.search(text)
    dur = 0.0
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return dur, ("Audio:" in text)


def normalize_video(src: Path, dst: Path, max_seconds: float = 0,
                    min_seconds: float = 0, caption: str = None,
                    _color_fix_tried: bool = False):
    dur, has_audio = probe(src)
    reps = 1
    if min_seconds > 0 and 0 < dur < min_seconds:
        # sub-2s clips flash by in a compilation: loop whole repetitions
        # until >= min_seconds (1.9s -> 2 plays = 3.8s, never truncated)
        reps = math.ceil(min_seconds / dur)
    args = ["-y"]
    if reps > 1:
        args += ["-stream_loop", str(reps - 1)]
    args += ["-i", str(src)]
    maps = ["-map", "0:v:0"]
    if has_audio:
        maps += ["-map", "0:a:0"]
    else:
        args += ["-f", "lavfi", "-i", SILENCE]
        maps += ["-map", "1:a:0"]
    vf = VF + _caption_filter(caption, Path(str(dst) + ".cap.txt"))
    if reps > 1:
        dur *= reps
    elif max_seconds > 0:
        args += ["-t", str(max_seconds)]
        dur = min(dur, max_seconds)
    args += maps + [
        "-vf", vf, "-c:v", "libx264", "-crf", "26", "-preset", "veryfast",
        "-c:a", "aac", "-ar", "44100", "-ac", "2", "-movflags", "+faststart",
        str(dst)]
    r = _run(args)
    if r.returncode != 0 or not dst.exists():
        # 2026-10-01：个别抖音视频的 H.264 色彩元数据是非法值（reserved），
        # ffmpeg 7.x 对它一律报 'Invalid color space' 拒绝转码。先用位流滤镜
        # 把元数据洗成 bt709（不重编码）再转一次；还失败才抛错。
        err = r.stderr.decode("utf-8", "replace")
        cleaned = None if _color_fix_tried else _sanitize_color(src)
        if cleaned is not None:
            try:
                log.info("retrying %s after color-metadata sanitize", src.name)
                return normalize_video(cleaned, dst, max_seconds, min_seconds,
                                       caption=caption, _color_fix_tried=True)
            finally:
                cleaned.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg video normalize failed: " + err[-300:])
    return dur


def normalize_image(img: Path, dst: Path, seconds: float, music: Path | None,
                    caption: str = None):
    args = ["-y", "-loop", "1", "-framerate", "30", "-t", str(seconds), "-i", str(img)]
    maps = ["-map", "0:v:0"]
    if music and music.exists():
        args += ["-i", str(music)]
        maps += ["-map", "1:a:0"]
    else:
        args += ["-f", "lavfi", "-i", SILENCE]
        maps += ["-map", "1:a:0"]
    args += maps + [
        "-t", str(seconds),
        "-vf", VF + _caption_filter(caption, Path(str(dst) + ".cap.txt")),
        "-c:v", "libx264", "-crf", "26", "-preset", "veryfast",
        "-c:a", "aac", "-ar", "44100", "-ac", "2", "-shortest",
        "-movflags", "+faststart", str(dst)]
    r = _run(args)
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError("ffmpeg image normalize failed: "
                           + r.stderr.decode("utf-8", "replace")[-300:])
    return seconds


def _mix_music(parts, music: Path, out: Path) -> bool:
    """把一个作品的所有片段拼成一条，再把背景乐连续铺满整个作品。

    2026-10-01 用户要求：多图共用一个音乐，不要每换一张图就重播一次。
    2026-10-02 修 bug：音乐比作品短时不能用静音垫尾（88 图 176s + 30s 音乐
    曾导致 32s 之后整段无声）——改为 -stream_loop -1 无限循环音乐，
    amix duration=first 到作品结束为���，音乐无缝接续、不按图重播。
    片段自带的声音（动态视频/夹带视频）与背景乐混音，各自保持原音量。
    """
    lst = out.with_suffix(".list.txt")
    try:
        lst.write_text("".join("file '%s'\n" % p.as_posix() for p in parts),
                       encoding="utf-8")
    except OSError as e:
        log.warning("music list write failed: %s", e)
        return False
    af = ("[1:a]aresample=44100,aformat=channel_layouts=stereo,apad[m];"
          "[0:a][m]amix=inputs=2:duration=first:normalize=0[a]")
    args = ["-y", "-f", "concat", "-safe", "0", "-i", str(lst),
            "-stream_loop", "-1", "-i", str(music),
            "-filter_complex", af,
            "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-ar", "44100", "-ac", "2",
            "-movflags", "+faststart", str(out)]
    r = _run(args)
    if r.returncode != 0 or not out.exists():
        log.warning("music attach failed: %s",
                    r.stderr.decode("utf-8", "replace")[-200:])
        return False
    return True


def build_compilation(segments, out_path: Path, image_seconds: float = 2.0,
                      max_video_seconds: float = 0,
                      min_video_seconds: float = 0,
                      skipped: list | None = None):
    """segments: list of dicts {kind: 'video'|'images', path: Path (mp4 or
    post folder), music: Path|None}. Returns total seconds compiled.

    skipped: optional list — failed segment paths are appended so callers that
    delete media after a send know what never made it into the compilation.
    """
    tmp = Path(tempfile.mkdtemp(prefix="douyin_merge_"))
    total = 0.0
    try:
        parts = []
        for i, seg in enumerate(segments):
            caption = seg.get("caption") or ""
            # 2026-10-01：一个坏片段（ffmpeg 'Invalid argument/Conversion failed'）
            # 曾把整轮拼接炸掉、卡住整个待发送队列。这里跳过编不出来的片段继续合并，
            # 个别坏文件不再拖累其余视频。
            if seg["kind"] == "video":
                dst = tmp / f"seg_{i:03d}.mp4"
                try:
                    total += normalize_video(seg["path"], dst, max_video_seconds,
                                             min_video_seconds, caption=caption)
                    parts.append(dst)
                except Exception as e:
                    log.warning("skip bad segment %s: %s", seg["path"], e)
                    if skipped is not None:
                        skipped.append(seg["path"])
            else:
                imgs = sorted(seg["path"].glob("img_*.*"))
                has_final_video = (seg["path"] / "video.mp4").exists()
                bad = False
                item_parts = []
                item_total = 0.0
                for j, img in enumerate(imgs):
                    # 多图作品整个不超过 30s（2026-10-02 用户规则）：放不下的图
                    # 直接截断，不再往后加
                    if item_total >= MAX_ITEM_SECONDS:
                        break
                    dst = tmp / f"seg_{i:03d}_{j:02d}.mp4"
                    m = re.match(r"img_(\d+)", img.stem)
                    live = (seg["path"] / f"live_{m.group(1)}.mp4") if m else None
                    has_live = live is not None and live.exists()
                    # 封面只放 1s（2026-10-01 用户要求）：后面跟着视频的图才是
                    # "封面"（动态照片的静态帧 / 单图特效贴的海报）；普通相册图保持
                    # image_seconds（默认 2s）
                    is_cover = has_live or (has_final_video and len(imgs) == 1)
                    show = 1.0 if is_cover else image_seconds
                    if item_total + show > MAX_ITEM_SECONDS:
                        break
                    try:
                        # 图片段一律先出静音轨；背景乐在作品级统一铺（不按图重播）
                        total += normalize_image(img, dst, show,
                                                 None, caption=caption)
                        item_parts.append(dst)
                        item_total += show
                    except Exception as e:
                        log.warning("skip bad image %s: %s", img, e)
                        bad = True
                        if skipped is not None:
                            # 整个图文贴都别删——只要有一张没拼进去就没发完整
                            skipped.append(seg["path"])
                        break
                    # 动态照片：这张图的动态视频紧跟在它后面播（2s 下限循环、
                    # 30s 上限截断），按 img_XX.jpg -> live_XX.mp4 配对
                    if has_live and not bad:
                        remain = MAX_ITEM_SECONDS - item_total
                        if remain < (min_video_seconds or 2):
                            break
                        ldst = tmp / f"seg_{i:03d}_{j:02d}_live.mp4"
                        try:
                            got = normalize_video(live, ldst,
                                                  max_seconds=min(30.0, remain),
                                                  min_seconds=min_video_seconds or 2,
                                                  caption=caption)
                            item_parts.append(ldst)
                            item_total += got
                        except Exception as e:
                            log.warning("skip bad live-photo video %s: %s", live, e)
                            bad = True
                            if skipped is not None:
                                skipped.append(seg["path"])
                            break
                # 混合作品（几张图 + 一段视频）：视频排在图片之后，2s 下限
                # 整遍循环补足、30s 上限截断（2026-10-01 用户规则）。
                vid = seg["path"] / "video.mp4"
                if vid.exists() and not bad:
                    remain = MAX_ITEM_SECONDS - item_total
                    if remain < (min_video_seconds or 2):
                        log.info("item %s: 30s budget used up, skip trailing video", seg["path"].name)
                    else:
                        dst = tmp / f"seg_{i:03d}_v.mp4"
                        try:
                            total += normalize_video(vid, dst,
                                                     max_seconds=min(30.0, remain),
                                                     min_seconds=min_video_seconds or 2,
                                                     caption=caption)
                            item_parts.append(dst)
                        except Exception as e:
                            log.warning("skip bad video %s: %s", vid, e)
                            if skipped is not None:
                                skipped.append(seg["path"])
                # 背景乐整段铺一次（2026-10-01 用户要求：多图共用一个音乐，
                # 不要每换一张图就重播一次）。图段已是静音轨，这里把音乐连着
                # 铺满整个作品，并与片段自带声音（动态视频/夹带视频）混音
                music = seg.get("music")
                if item_parts and music and Path(music).exists():
                    merged = tmp / f"seg_{i:03d}_music.mp4"
                    if _mix_music(item_parts, Path(music), merged):
                        item_parts = [merged]
                parts.extend(item_parts)
        if not parts:
            raise RuntimeError("no segments to merge")
        lst = tmp / "list.txt"
        lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts),
                       encoding="utf-8")
        r = _run(["-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                  "-c", "copy", "-movflags", "+faststart", str(out_path)])
        if r.returncode != 0 or not out_path.exists():
            raise RuntimeError("ffmpeg concat failed: "
                               + r.stderr.decode("utf-8", "replace")[-300:])
        return total
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
