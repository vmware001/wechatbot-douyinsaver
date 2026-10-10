"""Screenshot-based scroll-stability watcher for the douyin chat tab.

Every `interval_seconds`:
  - records scrollTop vs max (the definitive pinned-at-bottom metric)
  - captures a screenshot and diffs it against the previous one
  - if the chat tab crashed (chrome-error URL), navigates it back

Interpretation (user's heuristic): once pinned at the very bottom the view
cannot scroll further, so consecutive screenshots should differ only by the
trickle of new messages; a LARGE diff means the view is wandering (scrolling
up). The gap metric confirms it directly.

Read-only apart from the crash recovery; never touches WeChat.
"""
import queue  # stdlib preload
import base64
import json
import logging
import os
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import websocket
from PIL import Image, ImageChops

BASE_DIR = Path(__file__).resolve().parent.parent
try:
    DEBUG = json.loads((BASE_DIR / "config" / "douyin_saver.json").read_text(
        encoding="utf-8")).get("debug_url", "http://127.0.0.1:9223")
except Exception:
    DEBUG = "http://127.0.0.1:9223"
INTERVAL = 180
SHOT_DIR = Path(tempfile.gettempdir()) / "scroll_watch"
LOG = Path(os.environ.get("DOUYIN_LIB_DIR", r"D:\douyin_lib")) / "douyin_saved" / "scroll_watch.log"

log = logging.getLogger("scroll_watch")


def ensure_singleton():
    """Kill the previous instance of this script via a PID file (same
    pattern as douyin_saver — two watchers would double-inject key events)."""
    import os
    import subprocess
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


def find_chat():
    tabs = json.load(urllib.request.urlopen(DEBUG + "/json", timeout=10))
    pages = [t for t in tabs if t.get("type") == "page"
             and "/chat" in t.get("url", "")]
    # prefer the saver's dedicated automation tab (never the user's own tab)
    try:
        want = json.loads((Path(tempfile.gettempdir()) / "douyin_auto_tab.json")
                          .read_text(encoding="utf-8")).get("id")
        hit = next((t for t in pages if t.get("id") == want), None)
        if hit:
            return hit
    except Exception:
        pass
    return pages[0] if pages else None


class Tab:
    def __init__(self, page):
        self.ws = websocket.create_connection(
            page["webSocketDebuggerUrl"], timeout=40, suppress_origin=True)
        self.mid = 0

    def send(self, method, params=None):
        self.mid += 1
        self.ws.send(json.dumps({"id": self.mid, "method": method,
                                 "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.mid:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"])[:150])
                return msg.get("result", {})

    def js(self, expr):
        r = self.send("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True})
        return r.get("result", {}).get("value")


def capture(tab):
    r = tab.send("Page.captureScreenshot", {"format": "jpeg", "quality": 60})
    return base64.b64decode(r["data"])


def diff_pct(a_bytes, b_bytes):
    import io
    ia = Image.open(io.BytesIO(a_bytes)).convert("L").resize((64, 64))
    ib = Image.open(io.BytesIO(b_bytes)).convert("L").resize((64, 64))
    diff = ImageChops.difference(ia, ib)
    hist = diff.histogram()
    total = sum(i * v for i, v in enumerate(hist))
    return total / (64 * 64 * 255) * 100


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(LOG, encoding="utf-8")])
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    log.info("scroll watcher started, interval=%ds", INTERVAL)
    prev_shot = None
    prev_time = None
    cycles = 0
    while True:
        try:
            cycles += 1
            page = find_chat()
            if not page:
                log.warning("no chat tab found")
                time.sleep(30)
                continue
            if cycles % 40 == 39:  # ~every 2h (memory hygiene; keeper is off)
                log.info("periodic tab reload (memory hygiene)")
                try:
                    t = Tab(page)
                    t.send("Page.reload", {"ignoreCache": False})
                    t.ws.close()
                except Exception as e:
                    log.warning("periodic reload failed: %s", e)
                time.sleep(8)
                continue
            tab = Tab(page)
            href = str(tab.js("location.href") or "")
            if "douyin.com/chat" not in href:
                log.warning("tab at %r — navigating back", href[:60])
                tab.send("Page.navigate", {"url": "https://www.douyin.com/chat"})
                time.sleep(8)
                continue
            scroll = tab.js('''
            (function(){var sc=document.querySelector(".messageMessageListlist");
            return sc?[Math.round(sc.scrollTop), sc.scrollHeight-sc.clientHeight]:null;})()
            ''')
            shot = capture(tab)
            now = datetime.now()
            name = now.strftime("%H%M%S") + ".jpg"
            (SHOT_DIR / name).write_bytes(shot)
            gap = None
            if isinstance(scroll, list):
                # inverted list: distance-from-latest == scrollTop itself
                gap = scroll[0]
            d = diff_pct(prev_shot, shot) if prev_shot else None
            verdict = ""
            if gap is not None:
                verdict = "PINNED" if gap <= 1500 else "DRIFTED"
            log.info("gap=%s diff=%s %s (%s)",
                     gap if gap is not None else "?",
                     f"{d:.1f}%" if d is not None else "-",
                     verdict, name)
            prev_shot = shot
            prev_time = now
            tab.ws.close()
        except Exception as e:
            log.error("cycle error: %s", e)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    ensure_singleton()
    main()
