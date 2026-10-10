# -*- coding: utf-8 -*-
"""
个人微信自动发送模块 - 接口版
版本：v3.0.0
创建日期：2025-09-11
功能：基于通用接口的个人微信自动发送功能（WeChat.exe/Weixin.exe）
"""

import difflib
import json
import logging
import os
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
from time import sleep
from typing import Dict, Any

import psutil
import pyautogui
import pyperclip
import win32api
import win32con
import win32gui
import win32process
import win32ts
from PIL import Image, ImageChops, ImageGrab

from core.wechat_sender.message_sender_interface import MessageSenderInterface, MessageSenderFactory
from core.wechat_sender.virtual_mic import VirtualMicInjector, MAX_VOICE_DURATION

# 配置日志
logger = logging.getLogger(__name__)

# 设置 pyautogui 全局配置（必须在任何 pyautogui 操作之前设置）
try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
# 声明 DPI 感知后 GetWindowRect 与鼠标坐标统一为物理像素，
# 避免 VM 缩放比例≠100% 时点击坐标偏移（点穿到别的窗口）

pyautogui.FAILSAFE = False  # 禁用 fail-safe，防止鼠标移动到角落时触发异常
# pyautogui.PAUSE = 0.1  # 每次操作后的默认暂停时间（秒）

# 语音消息相关常量
VOICE_MAX_DURATION = 60          # 微信语音消息最大时长（秒）
# 微信按住说话快捷键：长按右 Alt（按住期间注入音频，注入完成再松开）
# 注意：微信 4.x 的按住说话键是右 Alt，不是 Shift（实测确认）
VOICE_KEY_VK = 0xA5              # VK_RMENU 右 Alt
VOICE_KEY_SCAN = 0x38            # 右 Alt 扫描码（需配合 EXTENDEDKEY 标志）
VOICE_KEY_FLAGS = win32con.KEYEVENTF_EXTENDEDKEY
# 按下快捷键后、开始注入前的等待时间（秒）：等微信进入录音状态，避免语音开头被截断
VOICE_PRESS_TO_INJECT_DELAY = 0.5

# =========================
# 🖱️ 微信输入框取点（点击恢复输入焦点）
# =========================
# 旧实现取"窗口水平中点 + 距窗口底 8%"，实测会落到输入框下方工具栏那一行、点中
# "截图"按钮 → 弹出微信截图浮层 → 焦点护栏报错、这条消息丢失。
# 证据：logs/sender.log 2026-09-26 20:18:11 误点 (1059,719)。像素实测微信主窗口底部
# 34~48px 就是工具栏图标那一行（截图/表情等），输入框正文区在距底约 65~150px
# （约 150px 处是输入区上边框）；而窗口高 500px 时旧算法给出的 y 距底仅 40px，
# 正落在图标行上；窗口高 782px 时距底 63px，仍在工具栏可点区域内。
# 工具栏是固定像素高度的 UI（不随窗口高度等比例放大），所以 y 改用"距 client 底部
# 固定像素"（110px 落在 65~150px 正文区的中段）；x 取 client 宽度的 60%
# （落在聊天面板正文区，避开最左侧导航条与中间的会话列表——两者合计约占窗口宽度 35%）。
INPUT_BOX_X_FRACTION = 0.60          # 横向：client 宽度的 60%
INPUT_BOX_BOTTOM_PX = 110            # 纵向：距 client 底部像素数（@96 DPI，落在输入框正文区）
INPUT_BOX_TOOLBAR_GUARD_PX = 70      # 底部工具栏高度下限（@96 DPI），偏移量必须大于它
INPUT_BOX_MIN_TOP_FRACTION = 0.4     # 兜底：y 不得高于 client 高度 40% 处（窗口极矮时防止上爬）


def compute_input_box_point(client_rect, dpi: int = 96):
    """算微信主窗口"消息输入框正文区"的点击点（屏幕物理像素）。

    Args:
        client_rect: (left, top, right, bottom)，client 区域在屏幕上的物理像素坐标
        dpi: 窗口所在显示器 DPI（96 = 100% 缩放），底部工具栏高度按同比例缩放

    Returns:
        (x, y)
    """
    left, top, right, bottom = (int(v) for v in client_rect)
    width = max(1, right - left)
    height = max(1, bottom - top)
    scale = max(1.0, float(dpi or 96) / 96.0)

    x = left + int(width * INPUT_BOX_X_FRACTION)

    # 硬性保证偏移量大于底部工具栏高度：常量被调小时也不会重新点回工具栏
    offset = max(int(INPUT_BOX_BOTTOM_PX * scale),
                 int(INPUT_BOX_TOOLBAR_GUARD_PX * scale) + 1)
    y = bottom - offset
    y = max(y, top + int(height * INPUT_BOX_MIN_TOP_FRACTION))

    return (min(max(x, left + 1), right - 1), min(max(y, top + 1), bottom - 1))


def _get_window_dpi(hwnd) -> int:
    """取窗口所在显示器 DPI；拿不到（老系统/无效句柄）按 96 = 100% 处理。"""
    try:
        import ctypes
        dpi = int(ctypes.windll.user32.GetDpiForWindow(hwnd))
        return dpi if dpi > 0 else 96
    except Exception:
        return 96


# 微信截图浮层的窗口标题关键词（实测：误点工具栏"截图"后前台变成标题 'Weixin' 的顶层窗口）
SCREENSHOT_OVERLAY_TITLE_KEYWORDS = ("weixin", "微信", "截图")

# 微信主窗口类名优先级（越靠前越像真正的主窗口）：
#   '*WeChatMainWndForPC*' 只有微信 3.x 主窗口用，最可信；
#   微信 4.x 主窗口与截图浮层等同为 'Qt51514QWindowIcon'（实测主窗口类名/标题
#   'Qt51514QWindowIcon' / '微信'），所以同类里只能靠面积和标题再区分。
WECHAT_MAIN_WINDOW_CLASS_PRIORITY = ("WeChatMainWndForPC", "Qt51514QWindowIcon")
# 主窗口尺寸下限：比这更小的可见顶层窗口不可能是主窗口（实测微信主窗口最小约 760x520），
# 这类窗口是按钮/浮层/临时面板。
WECHAT_MIN_WINDOW_SIZE = (200, 200)
# 标题字面就是 'weixin' 的窗口更可能是截图浮层/网页搜索面板这类伪主窗口（实测误点工具栏
# "截图"后前台窗口标题正是 'Weixin'）。仅当同一优先级里存在标题不是它的候选时才排除，
# 避免英文界面下反而把真正的主窗口排掉。
WECHAT_SUSPICIOUS_ONLY_TITLES = ("weixin",)

# =========================
# 🔤 deepseek-tokenizer：按 token 边界切分粘贴文本
# =========================
_TOKENIZER_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "assets", "deepseek_tokenizer",
)
_tokenizer = None
_tokenizer_failed = False


def _get_tokenizer():
    """懒加载 deepseek tokenizer；加载失败返回 None（调用方退回随机字符分段）。"""
    global _tokenizer, _tokenizer_failed
    if _tokenizer is not None or _tokenizer_failed:
        return _tokenizer
    try:
        from deepseek_tokenizer import DeepSeekTokenizer
        _tokenizer = DeepSeekTokenizer.from_pretrained(_TOKENIZER_DIR)
        logger.info("[DEBUG] deepseek-tokenizer 加载成功")
    except Exception as exc:
        _tokenizer_failed = True
        logger.warning(f"[DEBUG] deepseek-tokenizer 加载失败，退回随机字符分段: {exc}")
    return _tokenizer


def _build_unicode_to_byte():
    """GPT-2 风格 bytes_to_unicode 的逆映射（deepseek tokenizer 同款）。"""
    bs = list(range(ord("!"), ord("~") + 1)) + list(range(ord("¡"), ord("¬") + 1)) + list(range(ord("®"), ord("ÿ") + 1))
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return {chr(c): b for b, c in zip(bs, cs)}


_UNICODE_TO_BYTE = _build_unicode_to_byte()


def _token_spans(text: str):
    """按 token 边界切文本。半个 UTF-8 字符的 token 自动与后续合并，
    保证每段都是可独立粘贴的合法文本。tokenizer 不可用时返回 None。"""
    tok = _get_tokenizer()
    if tok is None or not text:
        return None
    try:
        ids = tok.encode(text)
        token_strs = tok.convert_ids_to_tokens(ids)
        spans, buf = [], b""
        for ts in token_strs:
            buf += bytes(_UNICODE_TO_BYTE[c] for c in ts)
            try:
                spans.append(buf.decode("utf-8"))
                buf = b""
            except UnicodeDecodeError:
                continue
        if buf:
            spans.append(buf.decode("utf-8", errors="replace"))
        if "".join(spans) != text:
            return None
        return [s for s in spans if s]
    except Exception as exc:
        logger.warning(f"[DEBUG] token 切分失败，退回随机字符分段: {exc}")
        return None


# =========================
# 🏷️ 聊天区标题校验（确认"切过去的到底是哪个会话"）
# =========================
# 背景：切群只有 Ctrl+F + Enter，程序从不校验"进的是哪个会话"，已多次把消息发到别的群、
# 或把搜群名变成网页搜索。实测（%TEMP%\title_probe\probe_log.txt，8 档窗口尺寸
# 760x520~1600x1000 全部通过、OCR 结果一致且命中已知会话名）确认"截聊天区顶部标题条 +
# Windows.Media.Ocr 识别"这条路可靠，本文件内联其实现（不 import 那个临时验证脚本）：
#   * 布局是固定像素：最左导航条 60px、会话列表 240px（聊天区通常从窗口内 x=308 开始）；
#     但会话列表宽度用户可拖拽，所以必须运行时探测聊天区左边缘（_detect_chat_left），
#     探测失败才回退 308；
#   * 标题条在窗口内 y36..78；聊天区右侧的按钮/图标是右对齐的，距窗口右边缘恒为 137px，
#     裁剪框必须让开这一块；
#   * 必须 3 倍放大（LANCZOS）：1 倍下中文会漏字（"目标群" → "目 群"）；
#     不要二值化（实测识别出 0 行）；
#   * OCR 会在 CJK 之间插空格、把半角括号读成全角（）→ 必须归一化，且比对要容错；
#   * OCR 读空（被遮挡等）必须判为不匹配 → 中止发送（保守）；
#   * 单次约 0.4s（PowerShell 启动占大头，引擎本体 80~96ms）。
TITLE_CHAT_LEFT_FALLBACK = 308       # 探测失败时的聊天区左边缘（窗口内像素）
TITLE_LIST_LEFT = 68                 # 会话列表左边缘（导航条 60 + 窗口阴影 8）
TITLE_Y0 = 36                        # 标题条上边缘（窗口内像素）
TITLE_Y1 = 78                        # 标题条下边缘（窗口内像素）
TITLE_MAX_W = 400                    # 标题条最多截取宽度
TITLE_RIGHT_CONTROLS = 137           # 右侧右对齐控制条占位（必须让开）
TITLE_RIGHT_MARGIN = 12              # 再留一点安全间距
TITLE_OCR_SCALE = 4                  # OCR 前的放大倍数默认值；调用方一般用
                                     # _read_chat_title_best 在 3/4/5 倍间轮流读、取相似度最高的一次
                                     # （实测同一标题 3x 与 4x 互有胜负，见该方法注释）
TITLE_OCR_TIMEOUT = 20               # 单次 OCR 子进程超时（秒）
TITLE_MATCH_THRESHOLD_DEFAULT = 0.6  # 相似度阈值默认值（config.sender.title_match_threshold）

# Windows.Media.Ocr（zh-Hans-CN）辅助脚本：PowerShell 启动开销大但零依赖，够用。
_TITLE_OCR_PS = r'''param(
    [Parameter(Mandatory=$true)][string]$Path,
    [string]$Lang    = 'zh-Hans-CN',
    [string]$OutFile = ''
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Foundation, ContentType=WindowsRuntime]
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]
function Await($op, $t) {
    $m = $asTaskGeneric.MakeGenericMethod($t)
    $task = $m.Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    $task.Result
}
$engine = $null
try { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage((New-Object Windows.Globalization.Language $Lang)) } catch { $engine = $null }
if ($engine -eq $null) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
if ($engine -eq $null) {
    $j = @{ ok = $false; error = 'no ocr engine'; text = ''; ms = 0 } | ConvertTo-Json -Depth 4
    if ($OutFile) { $j | Out-File -FilePath $OutFile -Encoding utf8 } else { Write-Output $j }
    exit 2
}
$t0 = Get-Date
$file    = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync((Resolve-Path -LiteralPath $Path).Path)) ([Windows.Storage.StorageFile])
$stream  = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap  = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$res     = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$ms = [int]((Get-Date) - $t0).TotalMilliseconds
$lines = @()
foreach ($line in $res.Lines) { $lines += $line.Text }
$stream.Dispose()
$out = [pscustomobject]@{
    ok = $true; lang = $engine.RecognizerLanguage.LanguageTag; ms = $ms
    lineCount = $lines.Count; text = (($lines) -join "`n")
}
$j = $out | ConvertTo-Json -Depth 4
if ($OutFile) { $j | Out-File -FilePath $OutFile -Encoding utf8 } else { Write-Output $j }
'''

_title_ocr_lock = threading.Lock()
_title_ocr_script_path = None

# 带行框的 OCR 变体：在纯文本基础上额外输出每行的包围盒（由词框求并集），
# 用于"点击搜索下拉里与目标名匹配的那一行"（2026-09-30：下拉布局随搜索词类型变化，
# '文件传输助手'没有'最常使用'区，固定点第一行会点到'搜索网络结果'而打开网页面板）。
_TITLE_OCR_BOX_PS = r'''param(
    [Parameter(Mandatory=$true)][string]$Path,
    [string]$Lang    = 'zh-Hans-CN',
    [string]$OutFile = ''
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Foundation, ContentType=WindowsRuntime]
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]
function Await($op, $t) {
    $m = $asTaskGeneric.MakeGenericMethod($t)
    $task = $m.Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    $task.Result
}
$engine = $null
try { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage((New-Object Windows.Globalization.Language $Lang)) } catch { $engine = $null }
if ($engine -eq $null) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
if ($engine -eq $null) {
    $j = @{ ok = $false; error = 'no ocr engine'; lines = @() } | ConvertTo-Json -Depth 4
    if ($OutFile) { $j | Out-File -FilePath $OutFile -Encoding utf8 } else { Write-Output $j }
    exit 2
}
$file    = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync((Resolve-Path -LiteralPath $Path).Path)) ([Windows.Storage.StorageFile])
$stream  = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap  = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$res     = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$lines = @()
foreach ($line in $res.Lines) {
    $x0 = [double]::MaxValue; $y0 = [double]::MaxValue; $x1 = 0.0; $y1 = 0.0
    foreach ($wd in $line.Words) {
        $br = $wd.BoundingRect
        if ($br.X -lt $x0) { $x0 = $br.X }
        if ($br.Y -lt $y0) { $y0 = $br.Y }
        if (($br.X + $br.Width)  -gt $x1) { $x1 = $br.X + $br.Width }
        if (($br.Y + $br.Height) -gt $y1) { $y1 = $br.Y + $br.Height }
    }
    if ($x0 -eq [double]::MaxValue) { $x0 = 0; $y0 = 0; $x1 = 0; $y1 = 0 }
    $lines += ,([pscustomobject]@{ text = $line.Text; x = [int]$x0; y = [int]$y0;
                                    w = [int]($x1 - $x0); h = [int]($y1 - $y0) })
}
$stream.Dispose()
$out = [pscustomobject]@{ ok = $true; lineCount = $lines.Count; lines = $lines }
$j = $out | ConvertTo-Json -Depth 4
if ($OutFile) { $j | Out-File -FilePath $OutFile -Encoding utf8 } else { Write-Output $j }
'''


def _ocr_image_boxes(img) -> list:
    """用 Windows.Media.Ocr 识别 PIL 图像，返回 [{text,x,y,w,h}, ...]（图像内像素坐标）。

    失败一律返回 []。供搜索下拉选行使用；标题校验仍用纯文本版 _ocr_image。
    """
    ps1 = _ensure_title_ocr_script()
    if not ps1:
        return []
    box_ps1 = ps1.replace('ocr.ps1', 'ocr_box.ps1')
    if not os.path.isfile(box_ps1):
        try:
            with open(box_ps1, 'w', encoding='utf-8') as f:
                f.write(_TITLE_OCR_BOX_PS)
        except Exception as e:
            logger.warning(f"[搜索下拉] 写带框 OCR 脚本失败: {e}")
            return []
    tmp_dir = tempfile.mkdtemp(prefix='gsmps_dropdown_ocr_')
    png_path = os.path.join(tmp_dir, 'dropdown.png')
    json_path = os.path.join(tmp_dir, 'dropdown.json')
    try:
        img.save(png_path)
        cmd = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
               '-File', box_ps1, '-Path', png_path, '-OutFile', json_path]
        kwargs = {'capture_output': True, 'timeout': TITLE_OCR_TIMEOUT}
        if hasattr(subprocess, 'CREATE_NO_WINDOW'):
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        subprocess.run(cmd, **kwargs)
        with open(json_path, encoding='utf-8-sig') as f:
            data = json.load(f)
        if not data.get('ok'):
            logger.warning(f"[搜索下拉] 带框 OCR 引擎返回失败: {data.get('error') or data}")
            return []
        lines = data.get('lines') or []
        # ConvertTo-Json 在单行时可能退化成对象而不是数组
        if isinstance(lines, dict):
            lines = [lines]
        return [ln for ln in lines if isinstance(ln, dict)]
    except Exception as e:
        logger.warning(f"[搜索下拉] 带框 OCR 调用失败: {type(e).__name__}: {e}")
        return []
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _ensure_title_ocr_script() -> str:
    """把 OCR 辅助脚本写到临时目录（只写一次），返回脚本路径；写不出返回 ""。"""
    global _title_ocr_script_path
    if _title_ocr_script_path and os.path.isfile(_title_ocr_script_path):
        return _title_ocr_script_path
    with _title_ocr_lock:
        if _title_ocr_script_path and os.path.isfile(_title_ocr_script_path):
            return _title_ocr_script_path
        try:
            ocr_dir = os.path.join(tempfile.gettempdir(), 'gsmps_title_ocr')
            os.makedirs(ocr_dir, exist_ok=True)
            path = os.path.join(ocr_dir, 'ocr.ps1')
            with open(path, 'w', encoding='utf-8') as f:
                f.write(_TITLE_OCR_PS)
            _title_ocr_script_path = path
            logger.info(f"[标题校验] OCR 辅助脚本已就绪: {path}")
        except Exception as e:
            logger.error(f"[标题校验] 写 OCR 辅助脚本失败: {e}")
            return ""
        return _title_ocr_script_path


def _ocr_image(img) -> str:
    """用 Windows.Media.Ocr 识别 PIL 图像，返回 OCR 原文。

    失败一律返回 ""（脚本写不出 / PowerShell 异常 / 超时 / 引擎不可用 / JSON 解析失败），
    调用方必须把它当作"读不出标题"来处理。
    """
    ps1 = _ensure_title_ocr_script()
    if not ps1:
        return ""
    tmp_dir = tempfile.mkdtemp(prefix='gsmps_title_ocr_')
    png_path = os.path.join(tmp_dir, 'title.png')
    json_path = os.path.join(tmp_dir, 'title.json')
    try:
        img.save(png_path)
        cmd = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
               '-File', ps1, '-Path', png_path, '-OutFile', json_path]
        kwargs = {'capture_output': True, 'timeout': TITLE_OCR_TIMEOUT}
        if hasattr(subprocess, 'CREATE_NO_WINDOW'):
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        subprocess.run(cmd, **kwargs)
        with open(json_path, encoding='utf-8-sig') as f:
            data = json.load(f)
        if not data.get('ok'):
            logger.warning(f"[标题校验] OCR 引擎返回失败: {data.get('error') or data}")
            return ""
        return str(data.get('text') or '')
    except Exception as e:
        logger.warning(f"[标题校验] OCR 调用失败: {type(e).__name__}: {e}")
        return ""
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _detect_chat_left(img) -> int:
    """运行时探测聊天区左边缘（窗口内像素坐标）；探测失败回退 TITLE_CHAT_LEFT_FALLBACK。

    做法与验证脚本一致：以窗口右下角像素作为聊天区背景色，自左向右找第一条"整列都接近
    该背景色"（该列每行的最大通道差值的平均值 < 6）的列。会话列表宽度用户可拖拽，所以
    不能硬编码 308。这里用纯 PIL 实现（PIL 已是本项目依赖，不额外引入 numpy）。
    """
    try:
        rgb = img.convert('RGB')
        w, h = rgb.size
        if w <= TITLE_LIST_LEFT + 2 or h < 8:
            return TITLE_CHAT_LEFT_FALLBACK
        bg = rgb.getpixel((w - 20, int(h * 0.9)))
        y0, y1 = int(h * 0.30), int(h * 0.85)
        rows = y1 - y0
        if rows < 4:
            return TITLE_CHAT_LEFT_FALLBACK
        strip = rgb.crop((0, y0, w, y1))
        diff = ImageChops.difference(strip, Image.new('RGB', strip.size, bg))
        r, g, b = diff.split()
        # 逐像素取三通道最大差，再按列切片求平均（sum < 6*rows 即"整列都接近背景色"）
        row_max = ImageChops.lighter(ImageChops.lighter(r, g), b).tobytes()
        limit = 6 * rows
        for x in range(TITLE_LIST_LEFT, w - 1):
            if sum(row_max[x::w]) < limit:
                return x
    except Exception as e:
        logger.warning(f"[标题校验] 探测聊天区左边缘失败: {e} → 回退 {TITLE_CHAT_LEFT_FALLBACK}")
    return TITLE_CHAT_LEFT_FALLBACK


def _title_crop_box(win_w: int, win_h: int, chat_left: int):
    """聊天区标题条裁剪框（窗口内坐标）；窗口太窄等非法情况抛 ValueError。"""
    x0 = max(0, int(chat_left))
    x1 = min(x0 + TITLE_MAX_W, int(win_w) - TITLE_RIGHT_CONTROLS - TITLE_RIGHT_MARGIN)
    if x1 <= x0:
        raise ValueError(f"窗口太窄,标题裁剪框无效: win_w={win_w}")
    y0 = min(TITLE_Y0, max(0, int(win_h) - 4))
    y1 = min(TITLE_Y1, int(win_h))
    return int(x0), int(y0), int(x1), int(y1)


# 标题归一化映射表。OCR 结果很脏：
#   * CJK 字符之间会被插空格（'目标群' → '目 标 群'）；
#   * 括号会被读成各种变体——同一张标题条，实测一次读成 '（'、下一次读成 '〔'
#     （'目标群〔56）'），所以所有"像左/右括号"的字符统一成半角 ()；
#   * 全角冒号/逗号/句号也要转半角；'·' 是 OCR 噪声，直接删。
# 注意只映射 CJK/全角括号变体，不动 ASCII 的 [ ] { } —— 真实会话名里可能带这些字符，
# 映射了会把 '群[A]' 和 '群(A)' 判成同一个。
_TITLE_CHAR_MAP = str.maketrans({
    '（': '(', '〔': '(', '【': '(', '「': '(', '『': '(', '〈': '(', '《': '(',
    '［': '(', '｛': '(', '﹙': '(', '⁽': '(',
    '）': ')', '〕': ')', '】': ')', '」': ')', '』': ')', '〉': ')', '》': ')',
    '］': ')', '｝': ')', '﹚': ')', '⁾': ')',
    '：': ':', '，': ',', '．': '.', '、': ',', '·': '',
})
# 尾部"未读数"后缀：'(56)' / '（56）' / '〔56）'（左右括号甚至可能被漏读）
_UNREAD_SUFFIX_RE = re.compile(r'[([{（〔【「『〈《［｛]\s*\d{1,4}\s*[)\]）〕】」』〉》］｝]?$')


def _norm_title(s: str) -> str:
    """标题归一化：去空白 + 括号/全角标点归一 + 去 '·' 噪声。

    实测同一张标题条的 OCR 原文可能是 '信 息 茅 房 （ 56 ）'，也可能开头括号读成 '〔'，
    所以比对前必须先归一化，且未读数后缀要按"括号变体 + 闭括号可缺"来剥。
    """
    s = re.sub(r'\s+', '', s or '')
    return s.translate(_TITLE_CHAR_MAP)


def _strip_unread_suffix(s: str) -> str:
    """去掉标题尾部的未读数后缀，如 '目标群(56)' / '目标群〔56）' → '目标群'。

    先做一次归一化再匹配，这样单独调用本函数时也能处理 OCR 的括号变体。
    OCR 偶尔把闭括号误读成汉字/符号（实测 '(58)' → '(5刀'），剥完标准后缀后
    再兜底剥一次"开括号+数字+少量噪声"的残尾。
    """
    s = _UNREAD_SUFFIX_RE.sub('', _norm_title(s))
    for _ in range(2):
        s = re.sub(r'[([{]\s*\d{1,4}\S{0,3}$', '', s)
    return s


def _title_similarity(a: str, b: str) -> float:
    """字符级相似度：相等=1；包含关系按长度比；否则用 difflib。"""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return min(len(a), len(b)) / max(len(a), len(b))
    return difflib.SequenceMatcher(None, a, b).ratio()


def _title_score(opened: str, target: str) -> float:
    """识别到的标题与目标名的相似度（0~1）；带/不带未读数后缀各算一遍取最高。"""
    o, t = _norm_title(opened), _norm_title(target)
    if not o or not t:
        return 0.0
    return max(_title_similarity(o, t),
               _title_similarity(_strip_unread_suffix(o), _strip_unread_suffix(t)),
               _title_similarity(o, _strip_unread_suffix(t)))


def _title_matches(opened: str, target: str,
                   threshold: float = TITLE_MATCH_THRESHOLD_DEFAULT) -> bool:
    """容错比对（不要求严格相等）；读不出标题（opened 为空）判定为不匹配。"""
    return _title_score(opened, target) >= float(threshold)


# =========================
# 🔎 "搜一搜"网页面板：识别 + 尽力恢复
# =========================
# 实测（2026-09-27，目标名不存在时）：Ctrl+F + 粘贴 + Enter 会把微信主窗口整块换成
# "搜一搜"网页搜索结果页，面板标题是 '<搜索词> · 搜一搜'（OCR 实测原文
# '一 亠 不 的 群 · 搜 一 搜'，归一化 '一亠不的群搜一搜'）。这种状态下：
#   * 聊天区已被整块替换，再按一次 Ctrl+F 只会越陷越深（实测把窗口卡死，后续正常
#     发送也全被拦下，需要人工点面板表头的 ✕ 才能恢复）→ 所以识别到面板就**不再重试**；
#   * 面板表头右侧有一个 ✕ 关闭按钮，点它可以把窗口恢复成普通会话视图（实测有效）。
#     ✕ 的位置是"距窗口右边缘 ≈152px + 标题带垂直中线"（W=965 时实测中心 ≈(813,55)），
#     按窗口宽度动态计算，不硬编码绝对坐标。**绝不按 Esc**（微信 4.x 主窗口收到 Esc
#     会把整个微信收进托盘）。
SEARCH_PANEL_TITLE_MARKER = "搜一搜"
# OCR 会把中间的"一"读成"亠/1/—"等，所以"搜 + 0~2 个字符 + 搜"也算面板特征
_SEARCH_PANEL_TITLE_RE = re.compile(r"搜.{0,2}搜")
SEARCH_PANEL_CLOSE_FROM_RIGHT = (235, 152, 300)  # ✕ 中心距窗口右边缘的候选值：
# 235 = 面板停靠在右侧时的实测（2026-09-30，窗口 1340 宽 → ✕ 在 1105）；
# 152 = 面板占满整个窗口时的实测（2026-09-27，965 宽 → 813）；
# 300 = 两种布局之间的兜底。逐个点，点完重新读标题验证，点错位置只是空点。
SEARCH_PANEL_CLOSE_Y_OFFSETS = (0, -6, 6)  # 相对标题带中线的垂直微调
SEARCH_PANEL_CLOSE_MAX_CLICKS = 5          # 最多点几次，避免盲点


def _looks_like_search_panel(title: str) -> bool:
    """判断读到的聊天区标题是不是"搜一搜"网页面板（而不是某个真实会话）。

    只用于"要不要重试/要不要恢复"的判断，不参与标题匹配本身——所以即使把某个真名叫
    "搜一搜"的会话误判成面板，也只会让它走"不重试直接中止"的保守分支。
    """
    t = _norm_title(title)
    if not t:
        return False
    if SEARCH_PANEL_TITLE_MARKER in t:
        return True
    return bool(_SEARCH_PANEL_TITLE_RE.search(t))


def _search_panel_close_points(win_w: int, win_h: int):
    """给出"搜一搜"面板表头 ✕ 的候选点击点（窗口内坐标），按可信度排序。
    先按各 x 候选的中心 y 轮一遍（覆盖停靠/全屏两种布局），再补垂直微调。"""
    win_w, win_h = int(win_w), int(win_h)
    y_center = (TITLE_Y0 + TITLE_Y1) // 2
    points = []
    for dy in SEARCH_PANEL_CLOSE_Y_OFFSETS:
        for from_right in SEARCH_PANEL_CLOSE_FROM_RIGHT:
            x = win_w - from_right
            y = y_center + dy
            if 2 < x < win_w - 2 and 2 < y < win_h - 2:
                points.append((x, y))
    return points


class WeChatSenderV3(MessageSenderInterface):
    """个人微信发送器 v3.0"""

    def __init__(self, config: Dict[str, Any] = None):
        """初始化个人微信发送器"""
        super().__init__(config)

        # 微信进程和窗口信息
        self.wechat_process = None
        self.wechat_pid = None
        self.wechat_pids = []
        self.main_window_hwnd = None

        # 默认配置
        self.process_names = ["WeChat.exe", "Weixin.exe", "wechat.exe"]
        self.default_group = config.get('default_group', '文件传输助手') if config else '存储统计报告群'

        # 粘贴方式：whole=整条一次粘贴（默认，最快、剪贴板注入次数最少）；
        # token=按 token 边界分段粘贴（段间随机停顿，仿人工节奏，但慢且注入频繁）。
        # config.json: sender.paste_mode
        _sender_cfg = (config or {}).get("sender") or {}
        if not isinstance(_sender_cfg, dict):
            _sender_cfg = {}
        try:
            self.paste_mode = str(_sender_cfg.get("paste_mode") or "whole").strip().lower()
        except Exception:
            self.paste_mode = "whole"

        # 聊天区标题校验：Ctrl+F 切群之后、以及发送粘贴之前，都截图识别聊天区标题并与
        # 目标名比对，不一致就中止（不粘贴、不按 Alt+S），防止"发到错的会话"。
        # config.json: sender.verify_chat_title / title_match_threshold / title_ocr_on_failure
        self.verify_chat_title = True
        self.title_match_threshold = TITLE_MATCH_THRESHOLD_DEFAULT
        self.title_ocr_on_failure = "abort"
        # 检测到"搜一搜"网页面板时，是否自动点面板表头的 ✕ 把窗口恢复成普通会话视图。
        # config.json: sender.recover_search_panel
        self.recover_search_panel = True
        try:
            self.verify_chat_title = bool(_sender_cfg.get("verify_chat_title", True))
            self.title_match_threshold = float(
                _sender_cfg.get("title_match_threshold", TITLE_MATCH_THRESHOLD_DEFAULT))
            self.title_ocr_on_failure = str(
                _sender_cfg.get("title_ocr_on_failure") or "abort").strip().lower()
            self.recover_search_panel = bool(_sender_cfg.get("recover_search_panel", True))
        except (TypeError, ValueError) as e:
            logger.warning(f"[标题校验] sender 配置读取失败，改用默认值: {e}")

        # 最近一次标题校验的结果（供 /wxPrepare 之类调用方给出"诚实"的响应/日志）
        self.last_title_verify = {}

        # dry-run：拦截所有对微信窗口的真实 UI 操作（用于全流程测试，可配合环境变量 WECHAT_SENDER_DRY_RUN=1）
        self.dry_run = bool((config or {}).get('dry_run', False)) or os.environ.get('WECHAT_SENDER_DRY_RUN', '0') == '1'
        # 多群支持：记住当前窗口停留在哪个群聊（_chat_ready_group）及对应窗口句柄。
        # 发送目标与缓存一致时跳过搜索直接发；不一致（或微信重启导致句柄变化）时
        # 自动走 Ctrl+F 搜索切换到目标群。
        self._chat_ready_group = None
        self._chat_ready_hwnd = None
        # 缓存有效期（秒）：距离上次成功发送超过该时长就不再信任缓存，
        # 重新搜索进群——覆盖用户中途手动切换会话的场景（无法被程序感知）。
        # 可用 config.sender.chat_cache_seconds 覆盖，默认 60。
        try:
            self._chat_cache_seconds = float(
                ((config or {}).get('sender') or {}).get('chat_cache_seconds', 60))
        except (TypeError, ValueError):
            self._chat_cache_seconds = 60.0
        self._chat_ready_at = None
        # 最近一次 search_group 是否"真的读过标题并确认"（缓存直接命中时为 False）。
        # 用于预切群（/wxPrepare）判断要不要补一次确认，从而给出诚实的成功/失败结论。
        self.last_search_verified = False

        # 语音注入设备从 config.voice_sender 读取；环境变量仍可作为部署时的覆盖项。
        voice_config = (config or {}).get('voice_sender', {})
        if not isinstance(voice_config, dict):
            voice_config = {}
        self.voice_playback_device = voice_config.get('playback_device_name')
        self.voice_capture_device = voice_config.get('capture_device_name')

    def initialize(self) -> bool:
        """初始化个人微信发送器"""
        try:
            logger.info("初始化个人微信发送器...")

            # 查找微信进程
            if not self.find_target_process():
                if self.dry_run:
                    logger.info("[dry-run] 未检测到微信进程，dry-run 模式下继续（不操作微信）")
                else:
                    logger.error("未找到个人微信进程")
                    return False

            # 查找微信窗口
            if not self._find_wechat_windows():
                if self.dry_run:
                    logger.info("[dry-run] 未检测到微信窗口，dry-run 模式下继续（不操作微信）")
                else:
                    logger.error("未找到个人微信窗口")
                    return False

            self.is_initialized = True
            logger.info("个人微信发送器初始化成功")
            return True

        except Exception as e:
            logger.error(f"初始化个人微信发送器失败: {e}")
            return False

    def find_target_process(self) -> bool:
        """查找个人微信进程"""
        try:
            wechat_processes = []
            for proc in psutil.process_iter(['pid', 'name', 'exe']):
                try:
                    proc_name = proc.info['name']
                    if any(name.lower() in proc_name.lower() for name in self.process_names):
                        wechat_processes.append(proc)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            if not wechat_processes:
                logger.error("未找到个人微信进程，请先启动微信")
                return False

            # 选择第一个微信进程
            self.wechat_process = wechat_processes[0]
            self.wechat_pid = self.wechat_process.pid
            self.wechat_pids = [proc.pid for proc in wechat_processes]
            logger.info(f"找到个人微信进程 PID: {self.wechat_pid}")
            return True

        except Exception as e:
            logger.error(f"查找个人微信进程失败: {e}")
            return False

    def _find_wechat_windows(self) -> bool:
        """查找个人微信主窗口。

        旧实现先找类名含 'WeChatMainWndForPC'（微信 3.x 的类名）的窗口，找不到就退化成
        "该进程第一个有标题的可见窗口"。微信 4.x 主窗口类名是 'Qt51514QWindowIcon'，
        于是永远走退化分支，会把微信截图浮层（标题 'Weixin'、同样是 Qt 类名）当成主窗口：
        logs/sender.log 出现过"使用备选个人微信窗口: Weixin"紧接着"无效的窗口句柄"。

        现在的取法（按优先级）：
          1) 丢掉尺寸明显过小的窗口（不可能是主窗口）；
          2) 丢掉"标题像截图浮层且面积不到最大窗口一半"的浮层；
          3) 按类名优先级分档（WeChatMainWndForPC > Qt51514QWindowIcon > 其它），
             取最像主窗口的那一档里面积最大的窗口；同档里若有标题不是 'Weixin' 的候选，
             优先它（'Weixin' 是实测的浮层标题）。
        """
        try:
            if not self.wechat_pid:
                logger.error("请先查找个人微信进程")
                return False

            # 枚举所有窗口
            windows_list = []
            win32gui.EnumWindows(self._enum_windows_callback, windows_list)

            # 查找属于微信进程的窗口
            pid_set = set(self.wechat_pids or [])
            pid_set.add(self.wechat_pid)
            wechat_windows = []
            for w in windows_list:
                if w['pid'] not in pid_set:
                    continue
                try:
                    left, top, right, bottom = win32gui.GetWindowRect(w['hwnd'])
                except Exception:
                    continue
                w = dict(w)
                w['width'] = max(0, right - left)
                w['height'] = max(0, bottom - top)
                w['area'] = w['width'] * w['height']
                wechat_windows.append(w)

            if not wechat_windows:
                logger.error("未找到个人微信窗口")
                return False

            # 排除 1) 尺寸明显过小的窗口；2) 标题像截图浮层且面积不到最大窗口一半的浮层。
            # （主窗口自己的标题也是"微信"，所以不能只看标题，必须配合面积判定。）
            max_area = max(w['area'] for w in wechat_windows)
            candidates, rejected = [], []
            for w in wechat_windows:
                if (w['width'] < WECHAT_MIN_WINDOW_SIZE[0]
                        or w['height'] < WECHAT_MIN_WINDOW_SIZE[1]):
                    rejected.append((w, '尺寸过小'))
                    continue
                title = (w['title'] or '').lower()
                if (any(k in title for k in SCREENSHOT_OVERLAY_TITLE_KEYWORDS)
                        and max_area and w['area'] < max_area * 0.5):
                    rejected.append((w, '疑似浮层'))
                    continue
                candidates.append(w)
            if rejected:
                logger.info("[DEBUG] 排除 %d 个窗口: %s", len(rejected),
                            ", ".join(f"{w['class']!r} {w['width']}x{w['height']} "
                                      f"title={w['title']!r}({why})" for w, why in rejected))
            if not candidates:
                logger.error("个人微信候选窗口全部被排除（尺寸过小/疑似浮层），无法确定主窗口")
                return False

            # 类名优先级分档：档位越小越像真主窗口；同档里取面积最大的
            def _tier(w):
                cls = w['class'] or ''
                for idx, name in enumerate(WECHAT_MAIN_WINDOW_CLASS_PRIORITY):
                    if name in cls:
                        return idx
                return len(WECHAT_MAIN_WINDOW_CLASS_PRIORITY)

            best_tier = min(_tier(w) for w in candidates)
            pool = [w for w in candidates if _tier(w) == best_tier]
            normal_titled = [w for w in pool if (w['title'] or '').strip().lower()
                             not in WECHAT_SUSPICIOUS_ONLY_TITLES]
            if normal_titled and len(normal_titled) < len(pool):
                logger.info("[DEBUG] 同档里优先标题不是 %s 的窗口（%s）",
                            WECHAT_SUSPICIOUS_ONLY_TITLES,
                            ", ".join(f"title={w['title']!r}" for w in pool if w not in normal_titled))
                pool = normal_titled
            best = max(pool, key=lambda w: w['area'])

            if not win32gui.IsWindow(best['hwnd']):
                logger.error(f"个人微信窗口句柄无效: {best['hwnd']}")
                return False

            self.main_window_hwnd = best['hwnd']
            logger.info(f"找到个人微信主窗口(类名档位={best_tier}, 候选{len(pool)}个): "
                        f"hwnd={best['hwnd']} {best['width']}x{best['height']} "
                        f"class={best['class']!r} title={best['title']!r}")
            if best_tier >= len(WECHAT_MAIN_WINDOW_CLASS_PRIORITY):
                logger.warning(f"[DEBUG] 没有窗口类名命中 {WECHAT_MAIN_WINDOW_CLASS_PRIORITY}，"
                               f"全部候选="
                               f"{[(hex(w['hwnd']), w['class'], w['width'], w['height']) for w in candidates]}")
            return True

        except Exception as e:
            logger.error(f"查找个人微信窗口失败: {e}")
            return False

    def _enum_windows_callback(self, hwnd, windows_list):
        """枚举窗口回调函数"""
        if win32gui.IsWindowVisible(hwnd):
            window_text = win32gui.GetWindowText(hwnd)
            class_name = win32gui.GetClassName(hwnd)

            # 获取窗口所属进程ID
            _, window_pid = win32process.GetWindowThreadProcessId(hwnd)

            windows_list.append({
                'hwnd': hwnd,
                'title': window_text,
                'class': class_name,
                'pid': window_pid
            })

    def _check_session_active(self) -> bool:
        """
        检查当前 Windows 会话是否处于活动状态（有交互式桌面）
        远程桌面断开后，会话会进入断开连接状态，此时无法进行 GUI 操作
        """
        try:
            # 获取当前进程的会话 ID
            current_session_id = win32ts.ProcessIdToSessionId(win32process.GetCurrentProcessId())
            
            # 枚举所有会话，检查当前会话状态
            sessions = win32ts.WTSEnumerateSessions(win32ts.WTS_CURRENT_SERVER_HANDLE)
            for session in sessions:
                if session['SessionId'] == current_session_id:
                    # WTSActive = 0 表示活动状态
                    # WTSDisconnected = 4 表示断开连接状态
                    state = session['State']
                    if state == win32ts.WTSActive:
                        return True
                    elif state == win32ts.WTSDisconnected:
                        logger.warning("检测到远程桌面已断开连接，GUI 操作将无法正常执行")
                        return False
                    else:
                        logger.warning(f"会话状态异常: {state}")
                        return False
            return True
        except Exception as e:
            logger.warning(f"检查会话状态失败: {e}，继续尝试执行")
            return True  # 检查失败时继续尝试

    def activate_application(self) -> bool:
        """激活个人微信窗口（使用三层降级策略）"""
        try:
            # dry-run：拦截所有对微信窗口的真实操作
            if self.dry_run:
                logger.info("[dry-run] 拦截微信窗口激活，不执行任何真实窗口操作")
                return True

            if not self.main_window_hwnd:
                logger.error("个人微信窗口句柄不存在")
                return False

            # 检查会话是否活动（远程桌面是否已断开）
            if not self._check_session_active():
                logger.error("远程桌面已断开，无法激活窗口。请重新连接远程桌面后再试。")
                return False

            # 检查窗口是否最小化，如果是则恢复
            if win32gui.IsIconic(self.main_window_hwnd):
                win32gui.ShowWindow(self.main_window_hwnd, win32con.SW_RESTORE)
                time.sleep(0.1)

            # 关键：模拟用户输入使当前进程获得前台权限
            # Windows 只允许前台进程调用 SetForegroundWindow
            pyautogui.press('alt')
            time.sleep(0.1)

            import ctypes
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            def _force_foreground(target):
                """AttachThreadInput 到当前前台线程后抢焦点，绕过前台锁"""
                fg = user32.GetForegroundWindow()
                fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
                this_thread = kernel32.GetCurrentThreadId()
                attached = False
                if fg_thread and fg_thread != this_thread:
                    attached = user32.AttachThreadInput(this_thread, fg_thread, True)
                try:
                    win32gui.BringWindowToTop(target)
                    win32gui.SetForegroundWindow(target)
                finally:
                    if attached:
                        user32.AttachThreadInput(this_thread, fg_thread, False)

            # 最多尝试4轮，每轮都必须用 GetForegroundWindow 实测验证——
            # SetForegroundWindow 调用成功不代表焦点真的切过去了（会被前台锁弹回）
            for attempt in range(1, 5):
                try:
                    win32gui.SetForegroundWindow(self.main_window_hwnd)
                except Exception:
                    try:
                        win32gui.BringWindowToTop(self.main_window_hwnd)
                        win32gui.SetForegroundWindow(self.main_window_hwnd)
                    except Exception:
                        pass
                time.sleep(0.2)

                if win32gui.GetForegroundWindow() == self.main_window_hwnd:
                    logger.info(f"个人微信窗口已激活 (第{attempt}次尝试，已实测验证)")
                    return True

                logger.warning(f"[DEBUG] 第{attempt}次激活后前台仍不是微信，用 AttachThreadInput 强抢...")
                _force_foreground(self.main_window_hwnd)
                time.sleep(0.2)
                if win32gui.GetForegroundWindow() == self.main_window_hwnd:
                    logger.info(f"个人微信窗口已激活 (AttachThreadInput 第{attempt}轮成功)")
                    return True

            logger.error("所有窗口激活方法均失败（4轮实测前台均不是微信）")
            return False

        except Exception as e:
            logger.error(f"激活个人微信窗口失败: {e}")
            return False

    def _foreground_info(self):
        """返回当前前台窗口 (hwnd, title, class)，用于发送前焦点校验与调试"""
        try:
            fh = win32gui.GetForegroundWindow()
            return fh, win32gui.GetWindowText(fh) or "", win32gui.GetClassName(fh) or ""
        except Exception as e:
            logger.debug(f"[DEBUG] 获取前台窗口失败: {e}")
            return None, "", ""

    def _ensure_wechat_focused(self, step: str) -> bool:
        """校验前台窗口是否为微信主窗口，不是则拒绝继续（防止按键落进别的窗口）"""
        fh, ftitle, fcls = self._foreground_info()
        ok = (fh == self.main_window_hwnd)
        logger.info(f"[DEBUG] {step} | 前台窗口 hwnd={fh} class={fcls} title={ftitle!r} | "
                    f"期望微信 hwnd={self.main_window_hwnd} | {'通过' if ok else '不通过→中止'}")
        return ok

    @staticmethod
    def _window_pid(hwnd) -> int:
        """取窗口所属进程 PID（拿不到返回 0）"""
        try:
            return int(win32process.GetWindowThreadProcessId(hwnd)[1])
        except Exception:
            return 0

    def _is_screenshot_overlay(self, fh, ftitle, fcls) -> bool:
        """判断前台窗口是不是"点击输入框"误触发的微信截图浮层。

        实测依据（logs/sender.log 2026-09-26 20:18:11）：误点输入区工具栏后前台是标题
        'Weixin' 的新顶层窗口，类名与主窗口相同（都是 Qt51514QWindowIcon），所以单靠
        类名区分不了。判定条件：
          1) 句柄绝对不是主窗口——这一条保证"任何情况下都不会对主窗口发 Esc"
             （主窗口收到 Esc 会把微信整个收进托盘，是踩过的坑）；
          2) 标题含 weixin / 微信 / 截图 关键词；
          3) 类名与主窗口不同，或该窗口属于微信进程（截图浮层是微信进程新建的顶层窗口）。
        """
        if not fh or fh == self.main_window_hwnd:
            return False
        title = (ftitle or "").lower()
        if not any(k in title for k in SCREENSHOT_OVERLAY_TITLE_KEYWORDS):
            return False
        try:
            main_cls = win32gui.GetClassName(self.main_window_hwnd) or ""
        except Exception:
            main_cls = ""
        if fcls and main_cls and fcls != main_cls:
            return True
        return self._window_pid(fh) in (self.wechat_pids or [])

    def _dismiss_screenshot_overlay(self, stage: str, attempt: int, fh, ftitle) -> None:
        """对微信截图浮层发一次 Esc 关掉它，保持屏幕干净。

        只允许对"前台不是主窗口"的截图浮层调用（调用方先过 _is_screenshot_overlay）：
        主窗口收到 Esc 会把微信收进托盘，绝不能对它发。
        """
        logger.warning(f"[DEBUG] {stage}:点击输入框误开微信截图浮层(第{attempt}次) "
                       f"hwnd={fh} title={ftitle!r} → 发Esc关闭"
                       + ("，随后重试一次" if attempt == 1 else ""))
        try:
            pyautogui.press('esc')
            time.sleep(0.3)
            logger.info(f"[DEBUG] {stage}:已发Esc关闭微信截图浮层(第{attempt}次)")
        except Exception as e:
            logger.warning(f"[DEBUG] {stage}:发Esc关闭微信截图浮层失败(第{attempt}次): {e}")

    def _input_box_point(self):
        """算当前微信主窗口输入框正文区的点击点（屏幕物理像素），返回 (x, y, 调试说明)"""
        hwnd = self.main_window_hwnd
        cl, ct, cr, cb = win32gui.GetClientRect(hwnd)   # client 左上角恒为 (0,0)
        left, top = win32gui.ClientToScreen(hwnd, (cl, ct))
        right, bottom = win32gui.ClientToScreen(hwnd, (cr, cb))
        dpi = _get_window_dpi(hwnd)
        x, y = compute_input_box_point((left, top, right, bottom), dpi)
        return x, y, f"client=({left},{top},{right},{bottom}) {right - left}x{bottom - top} dpi={dpi}"

    def _focus_input_box(self, stage: str) -> bool:
        """点击微信输入框正文区恢复输入焦点（搜索路径与发送路径共用）。

        返回 True：点击后前台仍是微信主窗口，调用方可以安全继续粘贴/按键。
        返回 False：焦点没回到微信，调用方必须中止本次流程（防止按键落进别的窗口）。

        点到输入区工具栏的"截图"按钮会弹出微信截图浮层，此时自愈：只对浮层发一次 Esc
        关掉它，再重新取点点击一次；重试仍失败则中止（浮层已被 Esc 关掉，不会留在屏幕上）。
        """
        if self.dry_run:
            logger.info(f"[dry-run] 拦截 {stage}:点击输入框恢复焦点，不执行真实点击")
            return True
        if not self.main_window_hwnd or not win32gui.IsWindow(self.main_window_hwnd):
            logger.error(f"[DEBUG] {stage}:微信主窗口句柄无效({self.main_window_hwnd}) → 中止")
            return False

        for attempt in (1, 2):
            try:
                x, y, geo = self._input_box_point()
            except Exception as e:
                logger.error(f"[DEBUG] {stage}:计算输入框点击坐标失败({e}) → 中止")
                return False

            try:
                pyautogui.click(x, y)
            except Exception as e:
                logger.error(f"[DEBUG] {stage}:点击输入框失败({x},{y}): {e} → 中止")
                return False
            time.sleep(0.3)

            fh, ftitle, fcls = self._foreground_info()
            if fh == self.main_window_hwnd:
                tag = "第1次" if attempt == 1 else f"第{attempt}次重试(截图浮层已自愈)"
                logger.info(f"[DEBUG] {stage}:点击输入框成功({tag}) 坐标=({x},{y}) {geo}")
                return True

            if self._is_screenshot_overlay(fh, ftitle, fcls):
                self._dismiss_screenshot_overlay(stage, attempt, fh, ftitle)
                if attempt == 1:
                    # 重试前确认微信主窗口回到前台：Esc 关掉浮层后前台通常回到主窗口，
                    # 万一是别的窗口，先走标准激活流程，避免重试的点击落到别的程序上
                    fh2, _, _ = self._foreground_info()
                    if fh2 != self.main_window_hwnd:
                        logger.info(f"[DEBUG] {stage}:截图浮层关闭后前台={fh2} 不是微信，"
                                    f"重新激活微信窗口后重试")
                        if not self.activate_application():
                            logger.error(f"[DEBUG] {stage}:截图浮层关闭后重新激活微信窗口失败 → 中止")
                            return False
                    continue
                logger.error(f"[DEBUG] {stage}:点击输入框误开微信截图浮层且重试后仍失败"
                             f"(第{attempt}次 hwnd={fh} title={ftitle!r}) → 中止发送")
                return False

            logger.error(f"[DEBUG] {stage}:点击输入框后焦点跑飞: hwnd={fh} "
                         f"title={ftitle!r} class={fcls} → 中止发送")
            return False
        return False

    def _clear_chat_cache(self):
        """清空"当前已在目标会话"缓存。

        切群/标题校验失败后，窗口实际停留在哪个会话是未知的，缓存不可信，
        必须清掉让下次发送强制重新搜索定位。
        """
        self._chat_ready_group = None
        self._chat_ready_hwnd = None
        self._chat_ready_at = None

    def _mark_chat_ready(self, group_name: str, hwnd=None):
        """写入"当前已在目标会话"缓存。

        只能在"标题校验确认过确实进了目标会话"之后调用——这是 2026-09-26 23:09 那次
        事故的教训：预切群当时谎报了"已搜索并进入个人微信群聊: 目标群"（窗口其实还停在
        私聊 root），缓存被写成目标名，随后的发送命中缓存直接跳过搜索，消息就发进了 root。
        """
        self._chat_ready_group = group_name
        self._chat_ready_hwnd = self.main_window_hwnd if hwnd is None else hwnd
        self._chat_ready_at = time.time()

    def _record_verify(self, stage: str, target: str, opened: str, ok: bool,
                       panel=None, recovered=None):
        """记录最近一次标题校验的完整结果（供 /wxPrepare 给出诚实响应、便于事后追溯）。"""
        self.last_title_verify = {
            "stage": stage,
            "target": _norm_title(target),
            "actual": _norm_title(opened),
            "score": round(_title_score(opened, target), 3),
            "ok": bool(ok),
            "verified": bool(self.verify_chat_title) and not self.dry_run,
            "panel": bool(_looks_like_search_panel(opened)) if panel is None else bool(panel),
            "recovered": recovered,
        }
        return bool(ok)

    def _capture_window_image(self, hwnd):
        """抓微信主窗口整窗图像（PIL.Image；失败返回 None）。

        优先 PrintWindow(PW_RENDERFULLCONTENT)：不受其它窗口遮挡影响（实测屏幕截图在窗口
        被遮挡时会读成空，从而被判为不匹配而中止发送）；PrintWindow 不可用时退回屏幕截图。
        """
        rect = None
        try:
            import win32ui
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            width, height = right - left, bottom - top
            rect = (left, top, width, height)
            if width <= 0 or height <= 0:
                logger.warning(f"[标题校验] 窗口尺寸非法: {width}x{height}")
                return None
            hdc = win32gui.GetWindowDC(hwnd)
            mfc_dc = win32ui.CreateDCFromHandle(hdc)
            save_dc = mfc_dc.CreateCompatibleDC()
            bmp = win32ui.CreateBitmap()
            try:
                bmp.CreateCompatibleBitmap(mfc_dc, width, height)
                save_dc.SelectObject(bmp)
                ok = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)
                if not ok:
                    logger.warning("[标题校验] PrintWindow 返回 0（可能抓不到内容），仍尝试识别")
                buf = bmp.GetBitmapBits(True)
                return Image.frombuffer('RGB', (width, height), buf,
                                        'raw', 'BGRX', 0, 1).copy()
            finally:
                win32gui.DeleteObject(bmp.GetHandle())
                save_dc.DeleteDC()
                mfc_dc.DeleteDC()
                win32gui.ReleaseDC(hwnd, hdc)
        except Exception as e:
            logger.warning(f"[标题校验] PrintWindow 截图失败: {type(e).__name__}: {e}")
        if rect is None:
            return None
        try:
            return pyautogui.screenshot(region=rect).convert('RGB')
        except Exception as e:
            logger.warning(f"[标题校验] 屏幕截图失败: {type(e).__name__}: {e}")
            return None

    def _read_chat_title(self, scale: int = None) -> str:
        """读取微信聊天区顶部标题，返回归一化后的字符串。

        任何异常、截图失败、OCR 读空都返回 ""（调用方必须按"读不出标题"保守处理）。
        前置条件：微信主窗口在前台——不在前台时截到的可能是别的窗口或被遮挡的内容，
        此时直接判为读不出。

        scale：OCR 前放大倍数。不同标题在不同倍数下漏字情况不同（实测同一张标题条
        '目标群(369)' 在 3x 下正确、4x 下读成 '密-(369)'；'目标群(59)' 反过来），
        所以调用方一般用 _read_chat_title_best 多倍数取最优。
        """
        hwnd = self.main_window_hwnd
        try:
            if not hwnd or not win32gui.IsWindow(hwnd):
                logger.warning(f"[标题校验] 主窗口句柄无效({hwnd})，读不出聊天区标题")
                return ""
            if win32gui.GetForegroundWindow() != hwnd:
                fh, ftitle, fcls = self._foreground_info()
                logger.warning(f"[标题校验] 微信主窗口不在前台(前台 hwnd={fh} class={fcls} "
                               f"title={ftitle!r})，读不出聊天区标题")
                return ""
            img = self._capture_window_image(hwnd)
            if img is None:
                logger.warning("[标题校验] 截图失败，读不出聊天区标题")
                return ""
            win_w, win_h = img.size
            chat_left = _detect_chat_left(img)
            box = _title_crop_box(win_w, win_h, chat_left)
            crop = img.crop(box)
            sc = int(scale or TITLE_OCR_SCALE)
            crop = crop.resize((crop.width * sc, crop.height * sc), Image.LANCZOS)
            raw = _ocr_image(crop)
            title = _norm_title(raw)
            logger.info(f"[标题校验] 窗口={win_w}x{win_h} 聊天区左边缘={chat_left} "
                        f"裁剪框={box} 放大={sc}x OCR原文={raw!r} 归一化={title!r}")
            return title
        except Exception as e:
            logger.warning(f"[标题校验] 读取聊天区标题失败: {type(e).__name__}: {e}")
            return ""

    def _read_chat_title_best(self, target: str, tries: int = 3,
                              scales=(3, 4, 5), interval: float = 0.4) -> str:
        """连读几次标题（每轮换不同放大倍数），返回与目标相似度最高的一次读数。

        两重不确定性都要压掉：
          * 切群动画期间标题条可能还没渲染完（隔 0.6s 再读就正确，实测 2026-09-30）；
          * OCR 放大倍数对不同标题各有失灵（3x / 4x / 5x 实测互有胜负）。
        所以多轮 × 多倍数取最优；读到面板特征或相似度已达标则提前返回。
        """
        best, best_score = "", -1.0
        thr = float(self.title_match_threshold)
        n = max(1, tries)
        for i in range(n):
            if i:
                time.sleep(interval)
            title = self._read_chat_title(scale=scales[i % len(scales)])
            if _looks_like_search_panel(title):
                return title
            score = _title_score(title, target) if title else 0.0
            # 同分时取更长的读数（漏字/截断的读数一般更短，信息更少）
            if score > best_score or (score == best_score and len(title) > len(best)):
                best, best_score = title, score
            if score >= thr:
                return title
        if best_score < thr:
            logger.info(f"[标题校验] 连读{n}次(倍数{list(scales)})取最优: {best!r} "
                        f"相似度={best_score:.2f}")
        return best

    def _close_sousuo_popups(self):
        """关掉"搜一搜"弹出的独立网页窗口（Chrome_WidgetWin 类、标题恰好是'微信'）。

        这种弹窗属于微信的内嵌浏览器进程（WeChatAppEx），会抢占前台导致主窗口
        前台检查失败。标题精确匹配 '微信' 以避免误关真正的 Chrome 窗口。
        """
        popups = []

        def cb(h, _):
            try:
                if not win32gui.IsWindowVisible(h):
                    return True
                if not win32gui.GetClassName(h).startswith("Chrome_WidgetWin"):
                    return True
                if (win32gui.GetWindowText(h) or "") != "微信":
                    return True
                popups.append(h)
            except Exception:
                pass
            return True

        try:
            win32gui.EnumWindows(cb, None)
        except Exception:
            return
        for h in popups:
            try:
                win32gui.PostMessage(h, win32con.WM_CLOSE, 0, 0)
                logger.info(f"[标题校验] 已关闭“搜一搜”独立网页窗口 hwnd={h}")
            except Exception:
                pass
        if popups:
            time.sleep(0.8)

    def _recover_from_search_panel(self, stage: str):
        """尽力把停在"搜一搜"网页面板上的微信主窗口恢复成普通会话视图。

        只做一件事：在面板表头的 ✕ 关闭按钮上点一下（坐标按窗口宽度动态算，见
        _search_panel_close_points），点完重新读标题确认面板确实没了。**绝不按 Esc**
        （微信 4.x 主窗口收到 Esc 会把整个微信收进托盘，是踩过的坑）。

        返回值：True 已恢复 / False 试过但没成功 / None 没做（配置关闭、dry-run、
        窗口不在前台、坐标不可用、读不出标题无法判断）。
        """
        if not getattr(self, "recover_search_panel", True):
            logger.warning(f"[标题校验] {stage}: 检测到“搜一搜”网页面板，但 "
                           f"sender.recover_search_panel=false → 只记日志、不点击；"
                           f"请人工点面板表头右侧的“✕”关闭")
            return None
        if self.dry_run:
            logger.info(f"[dry-run] {stage}: 检测到“搜一搜”面板，dry-run 不点击")
            return None
        hwnd = self.main_window_hwnd
        try:
            if not hwnd or not win32gui.IsWindow(hwnd):
                logger.warning(f"[标题校验] {stage}: 主窗口句柄无效，放弃自动关闭“搜一搜”面板")
                return None
            # 先关掉抢占前台的“搜一搜”独立网页弹窗（标题恰为'微信'的 Chrome_WidgetWin），
            # 否则主窗口永远过不了前台检查、自愈永远不生效
            self._close_sousuo_popups()
            if win32gui.GetForegroundWindow() != hwnd:
                # 面板/弹窗把前台抢走了——主动把主窗口拉回前台再复查一次
                try:
                    self.activate_application()
                    time.sleep(0.5)
                except Exception as e:
                    logger.warning(f"[标题校验] {stage}: 激活主窗口失败: {type(e).__name__}: {e}")
            if win32gui.GetForegroundWindow() != hwnd:
                logger.warning(f"[标题校验] {stage}: 微信主窗口不在前台，放弃自动关闭"
                               f"“搜一搜”面板（不往非前台窗口点鼠标）")
                return None
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            win_w, win_h = right - left, bottom - top
            points = _search_panel_close_points(win_w, win_h)[:SEARCH_PANEL_CLOSE_MAX_CLICKS]
            if not points:
                logger.warning(f"[标题校验] {stage}: 窗口 {win_w}x{win_h} 算不出 ✕ 候选点，"
                               f"放弃自动关闭“搜一搜”面板")
                return None
            logger.warning(f"[标题校验] {stage}: 检测到“搜一搜”网页面板 → 尝试点面板表头 ✕ 关闭"
                           f"(窗口 {win_w}x{win_h}，候选点={points})")
            for idx, (x, y) in enumerate(points, 1):
                # 面板活动期间会不定时弹出标题为'微信'的内嵌浏览器弹窗抢前台，
                # 每次点击前都清一遍并把主窗口拉回前台，否则读标题必然失败、
                # 自愈在第一轮就放弃（2026-10-01 实测）。
                self._close_sousuo_popups()
                if win32gui.GetForegroundWindow() != hwnd:
                    try:
                        self.activate_application()
                        time.sleep(0.4)
                    except Exception:
                        pass
                pyautogui.click(left + x, top + y)
                time.sleep(0.8)
                title = self._read_chat_title()
                if title and not _looks_like_search_panel(title):
                    logger.info(f"[标题校验] {stage}: 已自动关闭“搜一搜”面板"
                                f"(第{idx}次点击 窗口内({x},{y}) 生效)，当前会话标题={title!r}")
                    return True
                if not title:
                    # 读不出标题（截图被遮挡 / 弹窗再抢前台）——继续试下一个候选点，
                    # 全部试完仍不行才交给人工；绝不盲按 Esc。
                    logger.warning(f"[标题校验] {stage}: 第{idx}次点击(窗口内 {x},{y})后读不出"
                                   f"标题，继续尝试其余候选点")
                    continue
                logger.warning(f"[标题校验] {stage}: 第{idx}次点击(窗口内 {x},{y})后标题"
                               f"={title!r}，仍是“搜一搜”面板")
            logger.error(f"[标题校验] {stage}: 自动关闭“搜一搜”面板失败（已尝试窗口内 {points}），"
                         f"请人工点面板表头右侧的“✕”关闭")
            return False
        except Exception as e:
            logger.warning(f"[标题校验] {stage}: 自动关闭“搜一搜”面板异常: "
                           f"{type(e).__name__}: {e}")
            return None

    def _title_ok(self, opened: str, target: str, stage: str) -> bool:
        """判断读到的标题是否是目标会话；读空时按 sender.title_ocr_on_failure 处理。"""
        expected = _norm_title(target)
        if not opened:
            if self.title_ocr_on_failure == "abort":
                logger.error(f"[标题校验] {stage}: 读不出聊天区标题(OCR 空/被遮挡/截图失败)，"
                             f"目标={expected!r} → 按不匹配处理并中止"
                             f"（sender.title_ocr_on_failure=abort）")
                return False
            logger.warning(f"[标题校验] {stage}: 读不出聊天区标题，目标={expected!r}，但 "
                           f"sender.title_ocr_on_failure={self.title_ocr_on_failure} → 放行")
            return True
        score = _title_score(opened, target)
        if score >= float(self.title_match_threshold):
            logger.info(f"[标题校验] {stage}: 通过 目标={expected!r} 实际={opened!r} "
                        f"相似度={score:.2f} 阈值={self.title_match_threshold}")
            return True
        logger.error(f"[标题校验] {stage}: 不通过 目标={expected!r} 实际={opened!r} "
                     f"相似度={score:.2f} 阈值={self.title_match_threshold}")
        return False

    # 搜索下拉里的分区标题（归一化后精确匹配）；点击行时跳过这些"标题行"
    _DROPDOWN_SECTION_HEADERS = frozenset((
        '最常使用', '聊天记录', '搜索网络结果', '功能', '联系人', '群聊',
        '小程序', '公众号', '文章', '视频号', '表情', '服务',
    ))
    # 这些分区里的行点了不会进入目标会话（网页搜索/历史消息定位），只作兜底参考
    _DROPDOWN_BAD_SECTIONS = frozenset(('搜索网络结果', '聊天记录'))

    def _click_first_search_result(self, target: str = "") -> bool:
        """在搜索下拉窗口里找到与目标名匹配的那一行并点击。返回是否成功点击。

        搜索结果下拉是微信主窗口之外的独立 Qt 工具窗口（类名 Qt51514QWindowToolSaveBits，
        标题 'Weixin'，跟随搜索框弹出）。它在屏幕上是真实可见的顶层窗口，所以可以
        直接屏幕截图 + 带框 OCR + 屏幕坐标点击。

        2026-09-30 两次事故的教训：
          * 下拉的布局随搜索词类型变化：群聊/联系人有"最常使用"区、第一行即目标
            （区内偏移 y≈72）；但"文件传输助手"没有"最常使用"区，下拉从
            "搜索网络结果"区开始，固定点第一行会点到网络建议 → 打开“搜一搜”
            网页面板。所以不能盲点固定行，必须 OCR 找到文字与目标匹配的行；
          * 粘贴后下拉是逐步加载的（先出现半成品），必须等窗口位置稳定再截图。
        找不到下拉窗口或没有匹配行时返回 False，由调用方退回 Enter。
        """
        if not self.main_window_hwnd:
            return False
        try:
            main_rect = win32gui.GetWindowRect(self.main_window_hwnd)
        except Exception:
            return False
        pids = set(self.wechat_pids or [])
        if self.wechat_pid:
            pids.add(self.wechat_pid)

        def find_dropdown():
            found = []

            def cb(h, _):
                try:
                    if not win32gui.IsWindowVisible(h):
                        return True
                    if 'QWindowToolSaveBits' not in win32gui.GetClassName(h):
                        return True
                    _, pid = win32process.GetWindowThreadProcessId(h)
                    if pids and pid not in pids:
                        return True
                    r = win32gui.GetWindowRect(h)
                    w, ht = r[2] - r[0], r[3] - r[1]
                    # 下拉窗口：宽度几百 px、位于主窗口左上（搜索框）附近
                    if 200 < w < 800 and 200 < ht < 1200 and \
                            abs(r[0] - main_rect[0]) < 200 and \
                            0 < r[1] - main_rect[1] < 200:
                        found.append((h, r, w, ht))
                except Exception:
                    pass
                return True

            try:
                win32gui.EnumWindows(cb, None)
            except Exception:
                return None
            return found[0] if found else None

        # 等下拉窗口出现并稳定：连续两次轮询 rect 相同即认为布局稳定；
        # 若一直变化，超过 ~2.5s 也用最新 rect（结果流式加载时 rect 会一直变，
        # 死等稳定会白白放弃）。粘贴后下拉是逐步加载的，不等会截到半成品布局。
        last_rect = None
        stable = None
        polls = 0
        latest = None
        while polls < 20:  # 最多 ~5s
            polls += 1
            cand = find_dropdown()
            if cand:
                _, r, w, ht = cand
                latest = cand
                if r == last_rect:
                    stable = cand
                    break
                last_rect = r
                if polls >= 10:
                    stable = cand  # 已经等了 ~2.5s，就用当前布局
                    break
            time.sleep(0.25)
        if not stable:
            stable = latest
        if not stable:
            logger.warning("[DEBUG] 搜索:下拉窗口未出现 → 放弃本次切群")
            return False
        _, r, w, ht = stable

        # 截图 + 放大 2 倍再 OCR：小窗口下下拉字号很小，不放大 OCR 会把
        # '目标群' 读成 '0 集@航天'（相似度 0.67 的假命中）。
        # 点击门槛取得比标题阈值更严（≥0.75），宁可放弃也不点错行——
        # 点到"搜索网络结果"行就会打开"搜一搜"网页面板。
        click_thr = max(float(self.title_match_threshold), 0.75)
        for attempt in (1, 2):
            try:
                img = ImageGrab.grab(bbox=r)
            except Exception as e:
                logger.warning(f"[DEBUG] 搜索:下拉窗口截图失败: {type(e).__name__}: {e} → 放弃本次切群")
                return False
            big = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
            lines = _ocr_image_boxes(big)
            for ln in lines:  # 还原到下拉窗口内坐标
                for k in ('x', 'y', 'w', 'h'):
                    try:
                        ln[k] = int(ln.get(k, 0)) // 2
                    except Exception:
                        ln[k] = 0

            # 逐行归属分区（标题行只更新当前分区，不作为候选）；候选行要求与目标名
            # 相似度达标。行文本可能带尾巴（如'目标群 15:21'），先剥掉尾部日期/数字。
            #
            # 两条选行规则（2026-10-01 实测定稿）：
            #   A. 下拉以"最常使用"区开头（表头在顶部 ≤60px）→ 直接点它的第一行
            #      （区内偏移 ~73px）。这是微信的老布局，群聊/联系人都走这条路；
            #      群名本身 OCR 经常读不出（实测只读出头像 'G)'），位置比文字可靠。
            #   B. 否则（如'文件传输助手'这类没有"最常使用"区的）按 OCR 文字匹配行，
            #      但要求相似度 ≥ max(标题阈值, 0.75)，且在非"搜索网络结果/聊天记录"区。
            section = ''
            best = None  # (bad_section, -score, y, x_click, y_click, text)
            first_sec = None  # 顶部第一个分区表头 (name, y_mid)
            for ln in lines:
                text = str(ln.get('text') or '')
                norm = _norm_title(text)
                if not norm:
                    continue
                # 分区标题模糊匹配（OCR 把'搜索网络结果'读成'瘦索网疳结果'、
                # '最常使用'读成'最常便用'，所以阈值放到 0.6）；漏判标题行会把
                # 网络建议行/历史记录行当成可点候选，点错了就打开“搜一搜”面板。
                hdr = None
                for h in self._DROPDOWN_SECTION_HEADERS:
                    if _title_similarity(norm, h) >= 0.6:
                        hdr = h
                        break
                if hdr is not None:
                    section = hdr
                    if first_sec is None:
                        first_sec = (hdr, int(ln.get('y', 0)) + int(ln.get('h', 0)) // 2)
                    continue
                row = re.sub(r'[\d:：/\-—~]+$', '', norm)
                score = max(_title_score(row, target), _title_score(norm, target))
                if score < click_thr:
                    continue
                y_mid = int(ln.get('y', 0)) + int(ln.get('h', 0)) // 2
                cand = (section in self._DROPDOWN_BAD_SECTIONS, -score, y_mid,
                        r[0] + w // 2, r[1] + y_mid, text)
                if best is None or cand < best:
                    best = cand

            # 规则 A：底部第一句是"最常使用"且在顶部 → 点它下面第一行
            if first_sec and first_sec[0] == '最常使用' and first_sec[1] <= 60:
                y_click = r[1] + 73
                logger.info(f"[DEBUG] 搜索:下拉以'最常使用'区开头(表头 y={first_sec[1]})，"
                            f"点击其第一行 ({r[0] + w // 2},{y_click})")
                pyautogui.click(r[0] + w // 2, y_click)
                time.sleep(1.0)
                return True
            if best is None:
                logger.info(f"[DEBUG] 搜索:下拉 OCR 第{attempt}次没找到与 {target!r} 匹配的行"
                            f"(行数={len(lines)} 门槛={click_thr:.2f})")
                if attempt == 1:
                    time.sleep(0.8)  # 可能还在渲染，稍等再截一次
                    continue
                return False
            bad, neg_score, _, x, y, text = best
            if bad:
                # 只有'搜索网络结果/聊天记录'区匹配——点了进不了会话，直接放弃
                logger.warning(f"[DEBUG] 搜索:仅坏分区(搜索网络结果/聊天记录)里有匹配行"
                               f"({text!r}) → 放弃本次切群")
                return False
            logger.info(f"[DEBUG] 搜索:下拉 rect={r}，OCR 命中行 {text!r} 相似度={-neg_score:.2f}，"
                        f"点击 ({x},{y})")
            pyautogui.click(x, y)
            time.sleep(1.0)
            return True
        return False

    def _focus_global_search_box(self) -> bool:
        """点击微信左上角的全局搜索框（会话列表顶部），返回是否点了。

        不能靠 Ctrl+F：焦点在消息输入框时 Ctrl+F 进的是"搜索聊天记录"模式
        （见 _open_chat_by_search 的说明），下拉里没有群聊行。
        搜索框水平居中于会话列表，位置由运行时探测的会话列表宽度算；
        垂直位置实测在窗口内 y≈60（窗口内顶部 ~35~85 那条）。
        """
        hwnd = self.main_window_hwnd
        try:
            if not hwnd or not win32gui.IsWindow(hwnd):
                return False
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            img = self._capture_window_image(hwnd)
            if img is None:
                return False
            win_w, win_h = img.size
            chat_left = _detect_chat_left(img)
            x = (TITLE_LIST_LEFT + chat_left) // 2
            x = max(60, min(x, max(60, chat_left - 40)))
            y = 60
            if not (0 < y < win_h):
                return False
            logger.info(f"[DEBUG] 搜索:点击全局搜索框 窗口内({x},{y}) "
                        f"(窗口 {win_w}x{win_h} 会话列表宽 {chat_left})")
            pyautogui.click(left + x, top + y)
            time.sleep(0.6)
            return True
        except Exception as e:
            logger.warning(f"[DEBUG] 搜索:点全局搜索框异常: {type(e).__name__}: {e}")
            return False

    def _open_chat_by_search(self, group_name: str) -> bool:
        """执行一次「Ctrl+F → 粘贴群名 → Enter → 点回输入框」切群动作。

        返回 True 只代表动作序列跑完且焦点回到微信主窗口——进的是不是目标会话必须由调用方
        再做标题校验（所以本方法不能自称"已进入目标群"）。

        按键约束（都是实测事故换来的）：
          * Ctrl+F 每次搜索只按一次（不要连着按两次）；
          * 不用鼠标点击来"移除输入框焦点"：点击坐标稍有偏差就会点到别的窗口；
          * 绝不按 Esc：微信 4.x 主窗口收到 Esc 会把整个微信收进托盘（观感像被关掉了）。
        2026-10-01 复盘：这条搜索链路本身一直是好的（人工 Ctrl+F/Ctrl+V/Enter 没出过
        问题），当时真正坏的是标题校验的 OCR 误读（见 _read_chat_title_best），不要
        再动这里。
        """
        logger.info(f"搜索个人微信群聊(ctrl+f): {group_name}")

        # 激活微信窗口
        if not self.activate_application():
            return False

        # 焦点护栏：确认前台是微信才继续，否则中止（防止快捷键/粘贴落进别的窗口）
        if not self._ensure_wechat_focused(f"搜索[{group_name}]:按Ctrl+F前"):
            return False

        pyautogui.hotkey('ctrl', 'f')
        time.sleep(0.8)
        logger.info("[DEBUG] 搜索:已按Ctrl+F")

        # 输入群名搜索：剪贴板写入 + Ctrl+A 覆盖搜索框原有内容 + Ctrl+V 粘贴
        pyperclip.copy(group_name)
        pyautogui.hotkey('ctrl', 'a')
        time.sleep(0.2)
        pyautogui.hotkey('ctrl', 'v')
        # 等本地候选下拉列表加载完成，避免回车时候选还没出来变成全量搜索
        time.sleep(1.5)
        logger.info("[DEBUG] 搜索:已粘贴群名并等待候选加载")

        pyautogui.press('enter')
        time.sleep(1.0)
        logger.info("[DEBUG] 搜索:已按Enter选择结果")

        # 原实现这里用两次 win+d"退到桌面再回来"来激活输入框光标，会把整个桌面最小化再恢复，
        # 观感像微信被反复关闭打开，且最小化空档会打断后续粘贴。改为直接点击输入框正文区
        # 恢复焦点。取点方式见 compute_input_box_point 的注释；误开微信截图浮层会自动 Esc
        # 自愈（只对浮层按 Esc，主窗口不按）。
        if not self._focus_input_box("搜索"):
            return False
        return True

    def _verify_chat_title_with_retry(self, target: str):
        """切群后校验聊天区标题是否是目标会话；允许重试一次（重新走一遍搜索）。

        返回 (是否通过, 最后一次读到的标题)。不通过时调用方必须清缓存并中止发送。
        两条特殊规则：
          * 读到的标题像是"搜一搜"网页面板 → 直接判失败并中止，**不再重试**：实测第二次
            Ctrl+F 只会把微信越卡越深（2026-09-27 负面测试），此时改走 `_recover_from_search_panel`
            尽力把窗口点回普通会话视图；
          * 其它情况不匹配 → 重试一次（重走 Ctrl+F 搜索）。
        全程不按 Esc（见 _open_chat_by_search 的按键约束）。
        """
        if not self.verify_chat_title:
            logger.info("[标题校验] sender.verify_chat_title=false，跳过切群后校验")
            self._record_verify("切群后校验(已关闭)", target, "", True, panel=False)
            return True, ""
        if self.dry_run:
            logger.info("[dry-run] 跳过切群后标题校验")
            self._record_verify("切群后校验(dry-run)", target, "", True, panel=False)
            return True, ""

        opened = ""
        for attempt in (1, 2):
            stage = f"切群后校验(第{attempt}次)"
            opened = self._read_chat_title_best(target)
            panel = _looks_like_search_panel(opened)
            ok = self._title_ok(opened, target, stage)
            recovered = None
            if not ok and panel:
                logger.error(f"[标题校验] {stage}: 微信停在“搜一搜”网页面板上"
                             f"(目标={_norm_title(target)!r} 实际={opened!r}) → 不再重试 Ctrl+F"
                             f"（重试只会越陷越深），直接中止并尝试把窗口恢复成普通会话视图")
                recovered = self._recover_from_search_panel(stage)
            self._record_verify(stage, target, opened, ok, panel=panel, recovered=recovered)
            if ok:
                return True, opened
            if panel:
                return False, opened
            if attempt == 1:
                logger.warning(f"[标题校验] 第1次校验不通过(目标={_norm_title(target)!r} "
                               f"实际={opened!r})，重试一次搜索")
                if not self._open_chat_by_search(target):
                    logger.error(f"[标题校验] 重试搜索失败(激活/焦点护栏未通过) → 中止({target})")
                    return False, opened
            else:
                logger.error(f"[标题校验] 两次校验均不通过，判定进入的不是目标会话"
                             f"(目标={_norm_title(target)!r} 最后一次实际标题={opened!r}) → 中止发送")
        return False, opened

    def _verify_chat_title_before_send(self, target: str, stage: str = "发送前校验"):
        """发送路径在粘贴/按 Alt+S 之前再校验一次标题。返回 (是否通过, 读到的标题)。

        stage 只影响日志/记录里的标签（预切群确认也复用本方法）。
        """
        if not self.verify_chat_title:
            logger.info("[标题校验] sender.verify_chat_title=false，跳过发送前校验")
            self._record_verify(f"{stage}(已关闭)", target, "", True, panel=False)
            return True, ""
        if self.dry_run:
            logger.info("[dry-run] 跳过发送前标题校验")
            self._record_verify(f"{stage}(dry-run)", target, "", True, panel=False)
            return True, ""
        opened = self._read_chat_title_best(target)
        panel = _looks_like_search_panel(opened)
        ok = self._title_ok(opened, target, stage)
        recovered = None
        if not ok and panel:
            logger.error(f"[标题校验] {stage}: 微信停在“搜一搜”网页面板上 → 中止发送"
                         f"（不粘贴/不按键），并尝试把窗口恢复成普通会话视图")
            recovered = self._recover_from_search_panel(stage)
        self._record_verify(stage, target, opened, ok, panel=panel, recovered=recovered)
        return ok, opened

    def search_group(self, group_name: str) -> bool:
        """搜索并进入个人微信群聊"""
        try:
            # dry-run：拦截所有对微信的真实操作
            if self.dry_run:
                logger.info(f"[dry-run] 拦截群聊切换（{group_name}），不执行搜索/点击等真实操作")
                return True

            # 多群支持：缓存当前所在群聊。目标群与缓存一致、窗口句柄未变
            # （微信未重启）且距离上次发送未超缓存期时跳过搜索直接发；
            # 否则走下方真实 Ctrl+F 搜索切群。微信重启后 hwnd 会变，缓存自动失效。
            cache_fresh = (
                self._chat_ready_at is not None
                and (time.time() - self._chat_ready_at) <= self._chat_cache_seconds
            )
            if (self._chat_ready_group == group_name
                    and self._chat_ready_hwnd
                    and self._chat_ready_hwnd == self.main_window_hwnd
                    and win32gui.IsWindow(self._chat_ready_hwnd)
                    and cache_fresh):
                logger.info(f"[DEBUG] 当前已在目标群聊({group_name}),跳过搜索")
                # 缓存命中 = 本次没有重新读标题确认（读取交给发送路径的校验去做，避免热路径
                # 多花一次 OCR）。预切群要给出"真的切过去了"的诚实结论，会据此自己补一次确认。
                self.last_search_verified = False
                return True
            if (self._chat_ready_group == group_name and not cache_fresh
                    and self._chat_ready_at is not None):
                logger.info(
                    f"[DEBUG] 距上次发送已 {time.time() - self._chat_ready_at:.0f}s "
                    f"(>{self._chat_cache_seconds:.0f}s),重新搜索定位({group_name})")
            else:
                logger.info(f"[DEBUG] 需要切群: 当前={self._chat_ready_group!r} -> 目标={group_name!r}")

            if not self._open_chat_by_search(group_name):
                # 未走到校验：激活窗口失败，或点击输入框后焦点没回到微信（含截图浮层自愈重试失败）。
                # 此时窗口实际所在会话未知 → 清缓存并中止本次搜索。
                self._clear_chat_cache()
                self.last_search_verified = False
                logger.error(f"搜索个人微信群聊失败：激活窗口或点击输入框后焦点未回到微信，"
                             f"已中止({group_name})")
                return False

            # 标题校验：Ctrl+F+Enter 之后必须确认"进的是目标会话"，最多校验两次。
            ok, opened = self._verify_chat_title_with_retry(group_name)
            if not ok:
                self._clear_chat_cache()
                self.last_search_verified = False
                logger.error(f"搜索个人微信群聊失败：切群后标题校验不通过，目标={group_name!r} "
                             f"实际={opened!r} → 已清缓存并中止，本次不发送")
                return False

            logger.info(f"已搜索并进入个人微信群聊: {group_name}")
            # 只有校验确实读过标题且通过，才写缓存（见 _mark_chat_ready 的说明）
            self._mark_chat_ready(group_name)
            self.last_search_verified = bool(self.verify_chat_title) and not self.dry_run
            return True

        except Exception as e:
            # 异常可能发生在 Ctrl+F/粘贴/回车之后，此时窗口实际所在的会话未知，
            # 缓存已不可信——清除之，下次发送强制重新搜索定位
            self._clear_chat_cache()
            self.last_search_verified = False
            logger.error(f"搜索个人微信群聊失败: {e}")
            return False

    def prepare_group(self, target_group: str) -> bool:
        """预切群：LLM 生成回复期间提前搜索进入目标群聊，
        使随后的发送命中"当前群"缓存而免搜索（节省约 4~5 秒）。

        仅预热：发送路径仍会自行校验目标群，预热失败/切错都会在发送时自动纠正。
        返回值语义 = "是否确认已经停在目标会话"：只有读标题确认过才返回 True；
        缓存直接命中（本次没读标题）时会自己补一次确认，避免再出现 2026-09-26 23:09
        "日志说已进入目标群、窗口其实停在私聊 root"那种谎报成功。
        """
        try:
            target_group = str(target_group or "").strip()
            if not target_group:
                return False
            if self.dry_run:
                logger.info(f"[dry-run] 拦截预切群（{target_group}），不执行真实操作")
                return True
            logger.info(f"预切群请求：{target_group}")
            if not self.initialize():
                logger.warning(f"预切群失败：发送器初始化失败（{target_group}）")
                return False
            ok = self.search_group(target_group)
            if ok and not self.last_search_verified:
                # 缓存直接命中：search_group 没再读标题（热路径有意不重复读），
                # 但预热要给出"真的切过去了"的诚实结论 → 自己单独确认一次。
                logger.info(f"[标题校验] 预切群：缓存命中未重新搜索，单独确认一次标题"
                            f"（{target_group}）")
                ok, _ = self._verify_chat_title_before_send(target_group, stage="预切群确认")
                if not ok:
                    self._clear_chat_cache()
                    self.last_search_verified = False
            info = dict(self.last_title_verify or {})
            if ok:
                logger.info(f"预切群完成：{target_group}（确认标题={info.get('actual')!r} "
                            f"相似度={info.get('score')} 阶段={info.get('stage')!r}）")
            else:
                logger.warning(f"预切群失败：没有真正切到目标会话（目标={target_group!r} "
                               f"实际标题={info.get('actual')!r} 阶段={info.get('stage')!r} "
                               f"搜一搜面板={info.get('panel')} 已尝试恢复={info.get('recovered')}）"
                               f"→ 缓存未写入，随后的发送会重新搜索定位")
            return ok
        except Exception as e:
            logger.error(f"预切群异常({target_group}): {e}")
            return False
        finally:
            self.cleanup()

    def send_message(self, message: str, target_group: str = None) -> bool:
        """发送消息到个人微信
            
        Args:
            message: 要发送的消息内容
            target_group: 目标群聊名称（可选）
                
        Returns:
            bool: 发送是否成功
        """
        try:
            logger.info("准备发送消息到个人微信")

            # 如果指定了目标群聊，先搜索群聊
            if target_group:
                if not self.search_group(target_group):
                    logger.error(f"搜索群聊失败：{target_group}")
                    return False

            # 确保微信窗口处于前台
            if not self.activate_application():
                return False

            # 焦点护栏：前台不是微信就中止，防止消息粘贴进别的窗口
            if not self._ensure_wechat_focused("发送:粘贴前"):
                return False

            # 点击输入框正文区确保焦点在输入框上（否则粘贴会落空、Alt+S无效）。
            # 取点方式见 compute_input_box_point 的注释；点到别的窗口/误开截图浮层都
            # 会在 _focus_input_box 内部判定，返回 False 即中止，防止消息误发。
            if not self._focus_input_box("发送"):
                return False

            # 发送前再校验一次聊天区标题：切群可能失败，用户也可能中途手动切了会话。
            # 不匹配就在"粘贴之前"中止——不粘贴、不按 Alt+S（防误发的最后一道闸）。
            expected_chat = target_group or self._chat_ready_group
            if expected_chat:
                title_ok, opened = self._verify_chat_title_before_send(expected_chat)
                if not title_ok:
                    self._clear_chat_cache()
                    logger.error(f"发送中止：发送前标题校验不通过，目标={expected_chat!r} "
                                 f"实际={opened!r}（未粘贴、未按 Alt+S）")
                    return False
            else:
                logger.info("[标题校验] 未指定目标会话且无可信缓存，跳过发送前校验")

            # 粘贴方式见 self.paste_mode（config.json: sender.paste_mode）
            if getattr(self, "paste_mode", "whole") == "token":
                # 按 token 边界分段粘贴，段间随机停顿 0.05~0.15s
                chunks = self._split_into_chunks(message)
                for i, chunk in enumerate(chunks):
                    pyperclip.copy(chunk)
                    time.sleep(0.1)  # 剪贴板就绪的最小等待，删掉会导致粘贴落空
                    pyautogui.hotkey('ctrl', 'v')
                    if i < len(chunks) - 1:
                        time.sleep(random.uniform(0.05, 0.15))
                logger.info(f"[DEBUG] 发送:已分段粘贴消息(总长{len(message)},分{len(chunks)}段)")
            else:
                # 整条一次粘贴：一次剪贴板写入 + 一次 Ctrl+V
                pyperclip.copy(message)
                time.sleep(0.1)  # 剪贴板就绪的最小等待
                pyautogui.hotkey('ctrl', 'v')
                logger.info(f"[DEBUG] 发送:已整条粘贴消息(总长{len(message)})")
            time.sleep(0.5)

            # 发送消息（Alt+S）
            pyautogui.hotkey('alt', 's')
            time.sleep(1)
            logger.info("[DEBUG] 发送:已按Alt+S")

            logger.info("个人微信消息发送完成")
            # 发送成功：刷新群聊缓存时间戳（60s 内同群连发免搜索）
            if target_group and self._chat_ready_group == target_group:
                self._chat_ready_at = time.time()
            return True

        except Exception as e:
            logger.error(f"发送个人微信消息失败：{e}")
            return False

    @staticmethod
    def _split_into_chunks(message: str, min_len: int = 2, max_len: int = 5):
        """把消息切成小段分批粘贴，模拟真人输入节奏。

        全部长度都按 deepseek-tokenizer 的 token 边界切（中文按词/字、英文按词片）。
        短消息（≤100字）逐 token 一段；长消息把相邻 token 合并成 12~18 段，
        段边界仍落在 token 边界上。tokenizer 不可用时退回随机字符分段。
        """
        n = len(message)
        spans = _token_spans(message)
        if spans:
            if n <= 100:
                return spans
            target_chunks = random.randint(12, 18)
            target_len = max(4, n // target_chunks)
            chunks = []
            buf = ""
            for span in spans:
                buf += span
                if len(buf) >= target_len:
                    chunks.append(buf)
                    buf = ""
            if buf:
                chunks.append(buf)
            return chunks
        if n > 100:
            target_chunks = random.randint(12, 18)
            base = max(min_len, n // target_chunks)
            min_len = base
            max_len = int(base * 1.5) + 1
        chunks = []
        i = 0
        while i < n:
            step = random.randint(min_len, max_len)
            chunks.append(message[i:i + step])
            i += step
        return chunks

    def cleanup(self) -> bool:
        """清理资源"""
        try:
            logger.info("清理个人微信发送器资源")
            self.wechat_process = None
            self.wechat_pid = None
            self.main_window_hwnd = None
            self.is_initialized = False
            return True
        except Exception as e:
            logger.error(f"清理资源失败: {e}")
            return False

    def send_text(self, text: str, target_group: str) -> bool:
        """发送文本消息到指定的个人微信聊天对象
        
        Args:
            text: 要发送的文本内容
            target_group: 目标聊天对象名称
            
        Returns:
            bool: 发送是否成功
        """
        try:
            # 不再无条件"退到桌面"：窗口可见时直接在当前窗口发送，
            # 避免每条消息都最小化/恢复一轮（打断焦点导致内容断续甚至发送失败）。
            # 窗口最小化时 activate_application 内部会自行恢复。
            logger.info(f"开始发送文本消息到个人微信：{target_group}")

            # 初始化发送器
            if not self.initialize():
                logger.error("初始化个人微信发送器失败")
                return False

            # 发送消息
            if not self.send_message(text, target_group):
                logger.error("发送消息失败")
                return False

            logger.info(f"个人微信文本消息发送成功！目标：{target_group}")
            return True

        except Exception as e:
            logger.error(f"个人微信自动发送文本消息失败：{e}")
            return False
        finally:
            self.cleanup()

    def send_file(self, file_path: str, target_group: str) -> bool:
        """发送文件到指定的个人微信聊天对象
        
        Args:
            file_path: 本地文件的绝对路径
            target_group: 目标聊天对象名称
            
        Returns:
            bool: 发送是否成功
        """
        try:
            # 不再 win+d"退到桌面"：会把所有窗口最小化，体验差且可能打断焦点；
            # 窗口最小化时 activate_application 内部会自行恢复。
            logger.info(f"开始发送文件到个人微信：{target_group}")

            # 初始化发送器
            if not self.initialize():
                logger.error("初始化个人微信发送器失败")
                return False

            # 搜索目标聊天对象
            if not self.search_group(target_group):
                logger.error(f"搜索聊天对象失败：{target_group}")
                return False

            # 确保微信窗口处于前台
            if not self.activate_application():
                return False

            # 发送前再校验一次聊天区标题（与文本路径同一道闸，防止文件发进错的会话）：
            # 切群可能失败，用户也可能中途手动切了会话。不匹配就中止——
            # 不写剪贴板、不粘贴、不按 Alt+S。
            expected_chat = target_group or self._chat_ready_group
            if expected_chat:
                title_ok, opened = self._verify_chat_title_before_send(
                    expected_chat, stage="发送文件前校验")
                if not title_ok:
                    self._clear_chat_cache()
                    logger.error(f"发送文件中止：发送前标题校验不通过，目标={expected_chat!r} "
                                 f"实际={opened!r}（未写剪贴板、未粘贴、未按 Alt+S）")
                    return False
            else:
                logger.info("[标题校验] 未指定目标会话且无可信缓存，跳过发送文件前校验")

            # 将文件复制到剪贴板
            from core.wechat_sender.file_copy import copy_file_to_clipboard
            copy_file_to_clipboard(file_path)
            time.sleep(0.3)

            # 粘贴文件
            pyautogui.hotkey('ctrl', 'v')
            time.sleep(1)

            # 发送文件（Alt+S）
            pyautogui.hotkey('alt', 's')
            time.sleep(1)

            logger.info(f"个人微信文件发送成功！目标：{target_group}")
            if self._chat_ready_group == target_group:
                self._chat_ready_at = time.time()
            return True

        except Exception as e:
            logger.error(f"个人微信自动发送文件失败：{e}")
            return False
        finally:
            self.cleanup()

    def _press_voice_key(self):
        """长按右 Alt（微信按住说话）"""
        if self.dry_run:
            logger.info("[dry-run] 模拟长按右 Alt（不发送真实按键）")
            return
        # 单次长按到底，录音完成前不松开（避免快速按下-松开-再按下导致微信误判）
        win32api.keybd_event(VOICE_KEY_VK, VOICE_KEY_SCAN, VOICE_KEY_FLAGS, 0)
        logger.info("已长按右 Alt（开始录音）")

    def _release_voice_key(self):
        """松开右 Alt（结束录音）"""
        if self.dry_run:
            logger.info("[dry-run] 模拟松开右 Alt（不发送真实按键）")
            return
        win32api.keybd_event(
            VOICE_KEY_VK, VOICE_KEY_SCAN,
            VOICE_KEY_FLAGS | win32con.KEYEVENTF_KEYUP, 0,
        )
        logger.info("已松开右 Alt（结束录音）")

    def send_voice(self, voice_path: str, target_group: str, duration: float = None, start: float = None) -> bool:
        """发送语音消息到指定的个人微信聊天对象

        流程与文本/文件发送一致：先切换到目标群聊，然后长按右 Alt，
        同时将语音文件注入虚拟麦克风，注入完成后松开右 Alt。

        Args:
            voice_path: 语音文件绝对路径（wav/mp3/flac/ogg 等 soundfile 支持的格式）
            target_group: 目标聊天对象名称
            duration: 语音长度（秒），最大 60，支持小数；None 表示按住右 Alt 的时长 =
            音频实际长度（超过 60 秒的语音直接截断为 60 秒）
            start: 起始秒（从音频第 N 秒开始注入，支持小数），默认 0

        Returns:
            bool: 发送是否成功
        """
        try:
            # 不再 win+d"退到桌面"：会把所有窗口最小化，体验差且可能打断焦点；
            # 窗口最小化时 activate_application 内部会自行恢复。
            logger.info(f"开始发送语音消息到个人微信：{target_group}")

            # 校验语音文件
            if not voice_path or not os.path.isfile(voice_path):
                logger.error(f"语音文件不存在：{voice_path}")
                return False

            # 校验语音时长（最大 60 秒，支持小数）
            try:
                duration = float(duration) if duration is not None else None
            except (TypeError, ValueError):
                duration = None
            if duration is not None and (duration < 1 or duration > VOICE_MAX_DURATION):
                logger.error(f"语音时长超出限制（1~{VOICE_MAX_DURATION} 秒）：{duration}")
                return False

            # 校验起始秒（非负，支持小数；非法按 0 处理）
            try:
                start = float(start) if start is not None else None
            except (TypeError, ValueError):
                start = None
            if start is not None and start < 0:
                start = 0.0

            # 初始化发送器
            if not self.initialize():
                logger.error("初始化个人微信发送器失败")
                return False

            # 与其他消息类型一致的切换到群聊流程
            if not self.search_group(target_group):
                logger.error(f"搜索聊天对象失败：{target_group}")
                return False
            if not self.activate_application():
                logger.error("激活微信窗口失败")
                return False

            # 发送前再校验一次聊天区标题（与文本/文件路径同一道闸）：不匹配就中止——
            # 不按"按住说话"键、不注入音频（否则语音会录进错误的会话）。
            expected_chat = target_group or self._chat_ready_group
            if expected_chat:
                title_ok, opened = self._verify_chat_title_before_send(
                    expected_chat, stage="发送语音前校验")
                if not title_ok:
                    self._clear_chat_cache()
                    logger.error(f"发送语音中止：发送前标题校验不通过，目标={expected_chat!r} "
                                 f"实际={opened!r}（未按键、未注入音频）")
                    return False
            else:
                logger.info("[标题校验] 未指定目标会话且无可信缓存，跳过发送语音前校验")

            # 虚拟麦克风就绪检查
            injector = VirtualMicInjector(
                playback_device=self.voice_playback_device,
                capture_device=self.voice_capture_device,
            )
            if not injector.is_available():
                logger.error(
                    f"未找到虚拟麦克风播放设备（{injector.playback_keyword}）。"
                    f"请安装 VB-CABLE 并把微信语音输入设为 'CABLE Output'（或设置 WECHAT_VOICE_PLAYBACK_DEVICE）"
                )
                return False

            # 长按右 Alt 开始录音 -> 注入音频 -> 松开右 Alt 结束录音
            self._press_voice_key()
            try:
                # 按下快捷键后等 0.5s 再注入：等微信进入录音状态，避免语音开头被截断
                time.sleep(VOICE_PRESS_TO_INJECT_DELAY)
                played_seconds = injector.play(voice_path, duration=duration, start=start)
                logger.info(f"音频注入完成，实际注入时长：{played_seconds:.2f}s")
            finally:
                self._release_voice_key()

            logger.info(f"个人微信语音消息发送成功！目标：{target_group}")
            if self._chat_ready_group == target_group:
                self._chat_ready_at = time.time()
            return True

        except Exception as e:
            logger.error(f"个人微信自动发送语音消息失败：{e}", exc_info=True)
            return False
        finally:
            self.cleanup()

    def get_debug_info(self) -> Dict[str, Any]:
        """获取调试信息"""
        try:
            info = super().get_sender_info()

            # 添加个人微信特有信息
            if self.wechat_process:
                info["个人微信进程信息"] = {
                    "PID": self.wechat_pid,
                    "进程名": self.wechat_process.name(),
                    "可执行文件": getattr(self.wechat_process, 'exe', lambda: "无法获取")()
                }

            if self.main_window_hwnd:
                info["窗口信息"] = {
                    "窗口句柄": self.main_window_hwnd,
                    "窗口标题": win32gui.GetWindowText(self.main_window_hwnd),
                    "窗口类名": win32gui.GetClassName(self.main_window_hwnd)
                }

            return info

        except Exception as e:
            logger.error(f"获取调试信息失败: {e}")
            return {"错误": str(e)}

    # ==================== 向后兼容的方法 ====================
    def smart_search_group(self, group_name: str) -> bool:
        """向后兼容：智能搜索群聊"""
        return self.search_group(group_name)

    def send_message_to_current_chat(self, message: str) -> bool:
        """向后兼容：发送消息到当前聊天"""
        return self.send_message(message)

    def interactive_select_process(self) -> bool:
        """向后兼容：交互式选择进程（简化版）"""
        return self.find_target_process()

    def interactive_select_window(self) -> bool:
        """向后兼容：交互式选择窗口（简化版）"""
        return self._find_wechat_windows()


# 注册个人微信发送器到工厂
MessageSenderFactory.register_sender("wechat", WeChatSenderV3)


# ==================== 命令行接口 ====================
def main():
    """主程序入口"""
    import sys
    import json

    # 配置日志
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    sender = WeChatSenderV3()

    if len(sys.argv) > 1:
        command = sys.argv[1]

        if command == "send":
            # 发送文本消息到指定的聊天对象
            # 用法：uv run wechat_sender_v3.py send [聊天对象名] [聊天文本内容]
            if len(sys.argv) < 4:
                print("❌ 参数不足！用法：uv run wechat_sender_v3.py send [聊天对象名] [聊天文本内容]")
                sys.exit(1)

            target_name = sys.argv[2]
            message_text = sys.argv[3]

            success = sender.send_text(message_text, target_name)
            if success:
                print(f"✅ 消息发送成功！目标：{target_name}")
            else:
                print(f"❌ 消息发送失败！目标：{target_name}")
            sleep(0.1)
            print("=" * 60)

        elif command == "voice":
            # 发送语音消息到指定的聊天对象
            # 用法：uv run wechat_sender_v3.py voice [聊天对象名] [语音文件] [时长秒(可选)]
            if len(sys.argv) < 4:
                print("❌ 参数不足！用法：uv run wechat_sender_v3.py voice [聊天对象名] [语音文件] [时长秒(可选，最大60)]")
                sys.exit(1)

            target_name = sys.argv[2]
            voice_path = sys.argv[3]
            duration = None
            if len(sys.argv) > 4 and sys.argv[4].isdigit():
                duration = int(sys.argv[4])

            success = sender.send_voice(voice_path, target_name, duration)
            if success:
                print(f"✅ 语音消息发送成功！目标：{target_name}")
            else:
                print(f"❌ 语音消息发送失败！目标：{target_name}")
            sleep(0.1)
            print("=" * 60)

        elif command == "debug":
            # 退到桌面
            pyautogui.hotkey('win', 'd')
            # 获取调试信息
            sender.initialize()
            debug_info = sender.get_debug_info()
            print("=== 个人微信调试信息 ===")
            print(json.dumps(debug_info, ensure_ascii=False, indent=2))

        elif command == "test":
            test(sender)

        else:
            print("未知命令。可用命令：")
            print("  send [聊天对象名] [聊天文本内容] - 发送文本消息到个人微信")
            print("  voice [聊天对象名] [语音文件] [时长秒(可选)] - 发送语音消息到个人微信（需安装 VB-CABLE）")
            print("  debug - 获取调试信息")
            print("  test - 测试功能")
    else:
        print("个人微信自动发送工具 v3.0 (接口版)")
        print("用法:")
        print("  uv run wechat_sender_v3.py send [聊天对象名] [聊天文本内容] - 发送文本消息")
        print("  uv run wechat_sender_v3.py voice [聊天对象名] [语音文件] [时长秒(可选)] - 发送语音消息（需 VB-CABLE）")
        print("  uv run wechat_sender_v3.py debug - 获取调试信息")
        print("  uv run wechat_sender_v3.py test - 测试功能")


def test(sender: WeChatSenderV3):
    # 退到桌面
    pyautogui.hotkey('win', 'd')
    # 测试功能
    print("测试个人微信进程查找...")
    if sender.find_target_process():
        print("✅ 个人微信进程查找成功")

        print("测试个人微信窗口查找...")
        if sender._find_wechat_windows():
            print("✅ 个人微信窗口查找成功")

            print("测试窗口激活...")
            if sender.activate_application():
                print("✅ 窗口激活成功")
            else:
                print("❌ 窗口激活失败")
        else:
            print("❌ 个人微信窗口查找失败")
    else:
        print("❌ 个人微信进程查找失败")
    print("=" * 60)


if __name__ == "__main__":
    main()
