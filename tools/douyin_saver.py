"""Douyin group-chat video/image saver v2 — no clicking.

Pipeline per unseen card:
  1. Read the card's React fiber props -> parsedContent.itemId (aweme_id),
     author nickname and title.
  2. Stats + media URLs come PASSIVE-FIRST from the mitm capture
     (data/douyin_mitm/api_*.jsonl): the chat page itself fetches
     multi/aweme/detail when rendering a card, so no extra api call is
     needed. Fallback: call the aweme detail API from the page with an
     X-Bogus signature from byted_acrawler.frontierSign.
  3. Download the mp4 (or images) directly with Python requests (direct
     connection, bypassing the system proxy).

The chat page is only scrolled and read; no synthetic/user clicks are needed,
so the browser window can stay minimized. Progress is kept in manifest.json;
stats are also appended to a per-day stats.csv.

- one tab on douyin.com/chat is enough: the chat page itself carries the
  webmssdk signer and the profile cookies needed by the detail API.
Console output is English-only to avoid codepage issues.
"""
import queue  # stdlib preload keeps later imports fast
import csv
import json
import logging
import random
import re
import shutil
import string
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import requests
import websocket

import media_dedup
import video_merge

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "douyin_saver.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
    "Referer": "https://www.douyin.com/",
}

SCROLLER_SEL = ".messageMessageListlist"
CARD_SEL = "[class*=MessageItemShareAwemecontainer]"
CLOSE_SEL = ".commonModalFullScreenclose"

# covers already logged as re-share duplicates (persisted across restarts,
# otherwise every saver restart re-logs every old card in view as DUP)
_LOGGED_KNOWN_COVERS = set()
_LOGGED_COVERS_PATH = None


def load_logged_covers(save_root):
    global _LOGGED_COVERS_PATH
    _LOGGED_COVERS_PATH = Path(save_root) / "logged_covers.json"
    try:
        data = json.loads(_LOGGED_COVERS_PATH.read_text(encoding="utf-8"))
        _LOGGED_KNOWN_COVERS.update(data[-5000:])
    except Exception:
        pass


def _remember_logged_cover(key):
    _LOGGED_KNOWN_COVERS.add(key)
    if _LOGGED_COVERS_PATH is not None:
        try:
            _LOGGED_COVERS_PATH.write_text(
                json.dumps(list(_LOGGED_KNOWN_COVERS)[-5000:]),
                encoding="utf-8")
        except OSError:
            pass


# per-tab view state lives on ChatTab (prev_view_ids / have_prev_view):
# card identities (aweme uuid, falling back to cover key) visible in the
# previous harvest pass — used to tell a genuine re-share (a known card
# re-entering the view) apart from the original card simply sitting in the
# viewport while it ages. have_prev_view stays False until the tab's first
# harvest pass completes: everything visible at startup is "sitting in the
# view", not re-entering it, so the first pass must seed the set without
# DUP-logging (otherwise every restart logs a DUP burst)

# while the detail API is under systemic risk control (Argus 403s), stop ALL
# api calls for a cooldown window — constant probing keeps the rate-limit
# counter warm and delays recovery.
_API_MUTED_UNTIL = 0.0
API_MUTE_SECONDS = 600


def log_known_cover(card, manifest):
    """Log a purple DUP row when a known video is re-shared into the group.

    Only for videos saved >10 min ago (a card still in the viewport right
    after processing is not a re-share), and once per uuid (persisted).
    """
    key = card["key"]
    # dedup identity is the aweme uuid read from the card's fiber props —
    # a re-share keeps the same itemId, so uuid matching (not the cover
    # hash, not frame hashing) is what catches re-shares.
    iid = card.get("itemId") or manifest.data.get("covers", {}).get(key)
    dedup_key = iid or key
    if dedup_key in _LOGGED_KNOWN_COVERS:
        return
    rec = manifest.data.get("videos", {}).get(iid) or {}
    saved_at = rec.get("saved_at") or ""
    try:
        age = (datetime.now() - datetime.fromisoformat(saved_at)).total_seconds()
    except ValueError:
        return
    if age < 600:
        return
    st = rec.get("stats") or {}
    ratio = rec.get("ratio")
    if not st or ratio is None:
        return
    _remember_logged_cover(dedup_key)
    nick = str(rec.get("author") or "-").replace("[", "(").replace("]", ")")[:30]
    log.info("STATS %s id=%s digg=%s share=%s ratio=%s author=[%s] -> DUP (0.0 MB)",
             rec.get("type", "video"), iid or "-", st.get("digg", 0),
             st.get("share", 0), ratio, nick)

log = logging.getLogger("douyin_saver")


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


def random_ms_token():
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(107))


class CDP:
    """Thin CDP client bound to one page.

    websocket-client connections are not thread-safe: the bottom-keeper
    thread and the main loop share this client, so every send/recv pair is
    serialized with a class-level lock (interleaved frames would corrupt
    the protocol and wedge both threads).
    """

    _lock = None

    def __init__(self, debug_url, page):
        import threading
        if CDP._lock is None:
            CDP._lock = threading.Lock()
        self.debug_url = debug_url
        self.page = page
        self.ws = None
        self.mid = 0
        self.connect()

    @property
    def url(self):
        return self.page.get("url", "")

    def connect(self):
        self.ws = websocket.create_connection(
            self.page["webSocketDebuggerUrl"], timeout=40, suppress_origin=True)
        self.mid = 0

    def send(self, method, params=None):
        with CDP._lock:
            self.mid += 1
            self.ws.send(json.dumps({"id": self.mid, "method": method, "params": params or {}}))
            while True:
                msg = json.loads(self.ws.recv())
                if msg.get("id") == self.mid:
                    if "error" in msg:
                        raise RuntimeError(str(msg["error"])[:200])
                    return msg.get("result", {})

    def js(self, expr, await_promise=False):
        r = self.send("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True, "awaitPromise": await_promise})
        if r.get("exceptionDetails"):
            raise RuntimeError(json.dumps(r["exceptionDetails"])[:300])
        return r.get("result", {}).get("value")


class ChatTab:
    """One douyin chat tab pinned to a single group conversation."""

    def __init__(self, cdp):
        self.cdp = cdp
        self.group = None      # assigned group name (None = unassigned)
        self.ready = False     # scroller confirmed after opening the group
        self.prev_view_ids = set()
        self.have_prev_view = False


class Browser:
    def __init__(self, debug_url):
        self.debug_url = debug_url
        self.tabs = []   # list[ChatTab]
        self.api = None  # any chat tab carries the signer and cookies

    def _tabs(self):
        return json.load(urllib.request.urlopen(self.debug_url + "/json", timeout=10))

    def _chat_pages(self):
        return [t for t in self._tabs()
                if t.get("type") == "page" and "douyin.com/chat" in t.get("url", "")]

    def current_title(self, cdp):
        """The conversation this tab currently shows ('目标群(400)'
        -> '目标群')."""
        t = cdp.js("""
        (function(){
          var e = document.querySelector('.RightPanelHeadertitle');
          return e ? (e.textContent||'').trim() : '';
        })()
        """) or ""
        return re.sub(r"\s*\(\d+\)$", "", t).strip()

    def _new_tab(self):
        req = urllib.request.Request(
            self.debug_url + "/json/new?https%3A%2F%2Fwww.douyin.com%2Fchat",
            method="PUT")
        json.loads(urllib.request.urlopen(req, timeout=10).read())
        time.sleep(4)

    def connect(self, groups):
        pages = self._chat_pages()
        while len(pages) < len(groups):
            self._new_tab()
            pages = self._chat_pages()
        if not pages:
            raise RuntimeError("no douyin chat tab found")
        self.tabs = [ChatTab(CDP(self.debug_url, p)) for p in pages]
        self.api = self.tabs[0].cdp
        # adopt tabs already showing a wanted group (at most one tab per group)
        assigned = set()
        for tab in self.tabs:
            try:
                title = self.current_title(tab.cdp)
            except Exception:
                title = ""
            if title in groups and title not in assigned:
                tab.group = title
                tab.ready = True
                assigned.add(title)
        # assign the remaining groups to unassigned tabs
        for g in groups:
            if any(t.group == g for t in self.tabs):
                continue
            tab = next((t for t in self.tabs if t.group is None), None)
            if tab is None:
                self._new_tab()
                pages = self._chat_pages()
                tab = ChatTab(CDP(self.debug_url, pages[-1]))
                self.tabs.append(tab)
            tab.group = g
            tab.ready = False
        log.info("chat tabs: %s", ", ".join(
            "%s->%s" % (t.cdp.page.get("id", "")[:6], t.group or "-")
            for t in self.tabs))

    def ensure_connected(self, groups):
        if not self.tabs or self.api is None:
            self.connect(groups)
            return
        for tab in self.tabs:
            try:
                tab.cdp.js("1")
            except Exception:
                try:
                    tab.cdp.connect()
                except Exception:
                    # the page dict itself is stale (tab id gone) —
                    # reconnecting to it wedges forever, so force a full
                    # re-discovery on the next pass instead
                    self.tabs = []
                    self.api = None
                    raise

    # ---------- chat page ----------

    def close_extra_tabs(self):
        """单标签页模式：关掉多余的 douyin 聊天标签页，只留第一个轮转使用。

        2026-10-03 用户要求"只开一个标签页"：省资源、也少占任务栏。
        通过 CDP 调试口的 /json/close/<id> 关页；关不掉就只弃用不动它。
        """
        keep, extra = self.tabs[:1], self.tabs[1:]
        for tab in extra:
            try:
                pid = tab.cdp.page.get("id") or ""
                if pid:
                    urllib.request.urlopen(
                        self.debug_url + "/json/close/" + pid, timeout=5).read()
                    log.info("closed extra chat tab %s", pid[:6])
            except Exception as e:
                log.warning("close tab failed: %s", e)
        self.tabs = keep

    def ensure_group(self, tab):
        # The sticky sidebar item's background always differs from normal items,
        # so bg-based active detection false-negatives every pass. Only click
        # when we have never clicked this session or the chat view is REALLY
        # gone (the im-saas view briefly unmounts on heavy re-renders — debounce
        # so we don't reopen the conversation and jump to a stale scroll anchor).
        if tab.ready:
            try:
                for _ in range(3):
                    if self.current_title(tab.cdp) == tab.group and tab.cdp.js(
                            f"!!document.querySelector({json.dumps(SCROLLER_SEL)})"):
                        return True
                    time.sleep(0.7)
            except Exception:
                raise
        state = tab.cdp.js(f"""
        (function(){{
          var items = document.querySelectorAll('.conversationConversationItemtitle');
          for (var i=0; i<items.length; i++) {{
            if ((items[i].textContent||'').trim() === {json.dumps(tab.group)}) {{
              var r = items[i].getBoundingClientRect();
              return [Math.round(r.left+r.width/2), Math.round(r.top+r.height/2)];
            }}
          }}
          return null;
        }})()
        """)
        if not state:
            log.warning("group %r not found in sidebar", tab.group)
            return False
        log.info("clicking group %r in sidebar (tab %s)",
                 tab.group, tab.cdp.page.get("id", "")[:6])
        self.chat_click(tab, state[0], state[1])
        time.sleep(4)
        ok = bool(tab.cdp.js(
            f"!!document.querySelector({json.dumps(SCROLLER_SEL)})"))
        tab.ready = ok
        return ok

    def chat_click(self, tab, x, y):
        c = tab.cdp
        c.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        time.sleep(0.2)
        c.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y,
                                            "button": "left", "buttons": 1, "clickCount": 1,
                                            "pointerType": "mouse"})
        time.sleep(0.1)
        c.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y,
                                            "button": "left", "buttons": 0, "clickCount": 1,
                                            "pointerType": "mouse"})

    def close_leftover_overlay(self, tab):
        try:
            if tab.cdp.js(f"!!document.querySelector({json.dumps(CLOSE_SEL)})"):
                log.info("closing leftover player overlay")
                pt = tab.cdp.js(f"""
                (function(){{
                  var b = document.querySelector({json.dumps(CLOSE_SEL)});
                  var r = b.getBoundingClientRect();
                  return [Math.round(r.left+r.width/2), Math.round(r.top+r.height/2)];
                }})()
                """)
                if pt:
                    self.chat_click(tab, pt[0], pt[1])
                    time.sleep(1)
        except Exception as e:
            log.debug("overlay close skipped: %s", e)

    def cards_in_view(self, tab):
        """Return [{key, itemId, title, author}] parsed from card fiber props."""
        return tab.cdp.js(f"""
        (function(){{
          var out = [];
          document.querySelectorAll({json.dumps(CARD_SEL)}).forEach(function(c){{
            var img = c.querySelector('img[src*="douyinpic"]');
            var cover = img ? img.src : '';
            var m = cover.match(/douyinpic\\.com\\/[^/]+\\/([^~/?]+)/);
            var key = m ? m[1] : cover.slice(-40);
            if (!key) return;
            var itemId = '', title = '', author = '', uid = '';
            try {{
              var fk = Object.keys(c).find(function(k){{ return k.indexOf('__reactFiber$')===0; }});
              var fiber = fk ? c[fk] : null;
              var n = fiber, depth = 0;
              while (n && depth < 25) {{
                var p = n.memoizedProps;
                if (p && p.message && p.message.parsedContent) {{
                  var pc = p.message.parsedContent;
                  itemId = pc.itemId || '';
                  title = pc.content_title || '';
                  author = pc.content_name || '';
                  uid = pc.uid || '';
                  break;
                }}
                n = n.return; depth++;
              }}
            }} catch(e) {{}}
            if (!itemId) return;
            out.push({{key:key, itemId:String(itemId), title:title, author:author, uid:String(uid)}});
          }});
          return out;
        }})()
        """) or []

    def scroll_info(self, tab):
        return tab.cdp.js(f"""
        (function(){{
          var sc = document.querySelector({json.dumps(SCROLLER_SEL)});
          if (!sc) return null;
          return {{top: sc.scrollTop, max: sc.scrollHeight - sc.clientHeight}};
        }})()
        """)

    # ---------- detail API ----------

    def fetch_comments(self, item_id, count=20):
        """Top-ish comments for an aweme via the chat tab's signed fetch.

        Douyin's default comment order is recommendation-mixed (hot comments
        front-loaded, then chronological), so we pull `count` and let the
        caller sort/sample. One extra signed request per call.
        """
        expr = """
        (async function(){
          var msToken = %s;
          var qs = 'aweme_id=%s&cursor=0&count=%d&aid=6383&device_platform=webapp&channel=channel_pc_web&msToken=' + msToken;
          if (!window.byted_acrawler || !window.byted_acrawler.frontierSign) {
            return {fatal: 'no byted_acrawler on this tab'};
          }
          var sig = window.byted_acrawler.frontierSign('/aweme/v1/web/comment/list/?' + qs);
          var url = 'https://www.douyin.com/aweme/v1/web/comment/list/?' + qs
                    + '&X-Bogus=' + encodeURIComponent(sig['X-Bogus']);
          var ctrl = new AbortController();
          var timer = setTimeout(function(){ ctrl.abort(); }, 15000);
          try {
            var r = await fetch(url, {credentials:'include', signal:ctrl.signal,
                                      headers:{'Accept':'application/json'}});
            var t = await r.text();
            if (r.status !== 200) return {http: r.status, head: t.slice(0, 120)};
            var j = JSON.parse(t);
            if (j.status_code !== 0) return {status_code: j.status_code};
            return {ok: true, comments: (j.comments || []).map(function(c){
              // Array.from iterates code points — a plain .slice() can split
              // an emoji's surrogate pair and the lone surrogate then crashes
              // the manifest's utf-8 write downstream
              return {text: Array.from(c.text || '').slice(0, 80).join(''),
                      digg: c.digg_count || 0};
            })};
          } catch(e) {
            if (e && e.name === 'AbortError') return {timeout: true};
            return {jserror: String(e).slice(0, 120)};
          } finally {
            clearTimeout(timer);
          }
        })()
        """ % (json.dumps(random_ms_token()), item_id, count)
        res = self.api.js(expr, await_promise=True) or {}
        # belt and suspenders: drop any lone surrogates that slipped through
        for c in res.get("comments") or []:
            c["text"] = str(c.get("text") or "").encode("utf-8", "ignore") \
                .decode("utf-8", "ignore")
        return res

    def fetch_detail(self, item_id):
        """Return dict with stats/play_addrs/images or raise.

        Called on the chat tab itself (active tab keeps running in a minimized
        window, so no tab switching is required).
        """
        return self._fetch_detail_js(item_id)

    def _fetch_detail_js(self, item_id):
        expr = """
        (async function(){
          var msToken = %s;
          var qs = 'aweme_id=%s&aid=6383&device_platform=webapp&channel=channel_pc_web&msToken=' + msToken;
          if (!window.byted_acrawler || !window.byted_acrawler.frontierSign) {
            return {fatal: 'no byted_acrawler on this tab'};
          }
          var sig = window.byted_acrawler.frontierSign('/aweme/v1/web/aweme/detail/?' + qs);
          var url = 'https://www.douyin.com/aweme/v1/web/aweme/detail/?' + qs
                    + '&X-Bogus=' + encodeURIComponent(sig['X-Bogus']);
          var ctrl = new AbortController();
          var timer = setTimeout(function(){ ctrl.abort(); }, 15000);
          try {
            var r = await fetch(url, {credentials:'include', signal:ctrl.signal,
                                      headers:{'Accept':'application/json'}});
            var t = await r.text();
            if (r.status !== 200) return {http: r.status, head: t.slice(0, 120)};
            var j = JSON.parse(t);
            if (!j.aweme_detail) return {status_code: j.status_code, empty: t.slice(0, 150)};
            var a = j.aweme_detail;
            var v = a.video || {};
            return {
              ok: true,
              desc: a.desc || '',
              create_time: a.create_time || 0,
              is_aigc_media: a.is_aigc_media ? 1 : 0,
              statistics: a.statistics || {},
              play_addrs: (v.play_addr && v.play_addr.url_list || []),
              duration: v.duration || 0,
              images: (function(){
                var out = [];
                for (var i = 0; i < (a.images || []).length; i++) {
                  var im = a.images[i];
                  if (im.url_list && im.url_list[0]) out.push(im.url_list[0]);
                }
                return out;
              })(),
              image_videos: (function(){
                // 动态照片：动态部分藏在 images[i].video，顶层 play_addr 是背景音乐
                var out = [];
                function pick(list){
                  for (var i = 0; i < (list || []).length; i++) {
                    var u = list[i] || '';
                    if (u && u.indexOf('.mp3') < 0 && u.indexOf('ies-music') < 0) return u;
                  }
                  return '';
                }
                for (var i = 0; i < (a.images || []).length; i++) {
                  var im = a.images[i];
                  if (!(im.url_list && im.url_list[0])) continue;
                  var iv = im.video || {};
                  var u = '';
                  var brs = iv.bit_rate || [];
                  for (var b = 0; b < brs.length && !u; b++) {
                    u = pick((brs[b].play_addr && brs[b].play_addr.url_list) || []);
                  }
                  if (!u) u = pick((iv.play_addr && iv.play_addr.url_list) || []);
                  out.push(u);
                }
                return out;
              })(),
              music: (a.music && a.music.play_url && a.music.play_url.url_list) || [],
            };
          } catch(e) {
            if (e && e.name === 'AbortError') return {timeout: true};
            return {jserror: String(e).slice(0, 120)};
          } finally {
            clearTimeout(timer);
          }
        })()
        """ % (json.dumps(random_ms_token()), item_id)
        return self.api.js(expr, await_promise=True) or {}


class Manifest:
    def __init__(self, path):
        self.path = path
        self.data = {"videos": {}, "covers": {}, "failed": {}}
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text(encoding="utf-8")))
            except Exception as e:
                log.warning("manifest unreadable (%s), starting fresh", e)

    def cover_state(self, key):
        if key in self.data["covers"]:
            return "known"
        f = self.data["failed"].get(key)
        if isinstance(f, dict):
            count, last = f.get("count", 0), f.get("last")
        else:
            count, last = f or 0, None
        if count >= 3:
            # givenup is time-limited: failures during a systemic api block
            # are not the item's fault — retry after 4h instead of never.
            try:
                age = (datetime.now() - datetime.fromisoformat(last)).total_seconds() if last else 1e18
            except ValueError:
                age = 1e18
            if age < 4 * 3600:
                return "givenup"
        return "new"

    def record(self, cover_key, item_id, record, failed_inc=0):
        if item_id:
            self.data["covers"][cover_key] = item_id
            if record:
                old = self.data["videos"].get(item_id)
                # a re-seen item must not clobber the stored file list with an
                # empty one (same video shared twice in a row)
                if old and not record.get("files") and old.get("files"):
                    record = dict(record)
                    record["files"] = old["files"]
                    record["downloaded"] = old.get("downloaded", False)
                self.data["videos"][item_id] = record
            self.data["failed"].pop(cover_key, None)
        elif failed_inc:
            f = self.data["failed"].get(cover_key)
            count = (f.get("count", 0) if isinstance(f, dict) else (f or 0)) + failed_inc
            self.data["failed"][cover_key] = {
                "count": count, "last": datetime.now().isoformat(timespec="seconds")}
        self.save()

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)


def sanitize(name):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip(" ._")
    return name[:24] or "unknown"


_DIRECT = requests.Session()
# the system proxy now points at our mitm instance; CDN downloads do not
# need it and must not depend on it being alive.
_DIRECT.trust_env = False


def download(url, dest: Path, timeout=90, min_bytes=20 * 1024):
    dest.parent.mkdir(parents=True, exist_ok=True)
    with _DIRECT.get(url, headers=HEADERS, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 18):
                f.write(chunk)
    if dest.stat().st_size < min_bytes:
        dest.unlink(missing_ok=True)
        raise RuntimeError("file too small, discarded")


def pool_add_images(cfg, item_id, folder: Path, music: Path | None,
                    caption: str = ""):
    """Build a cover+video / 2s-per-image preview from an image post and pool it.

    Reuses the compilation renderer so the labeling page shows exactly what a
    compilation segment would look like (caption overlay included). Built to
    temp first, then handed to pool_add (dedup + cap)."""
    try:
        tmp = Path(tempfile.mkdtemp(prefix="pool_img_")) / f"{item_id}.mp4"
        video_merge.build_compilation(
            [{"kind": "images", "path": folder, "music": music,
              "caption": caption or ""}],
            tmp, image_seconds=2.0)
        ok = pool_add(cfg, item_id, tmp)
        tmp.unlink(missing_ok=True)
        return ok
    except Exception as e:
        log.warning("image-post pool video failed for %s: %s", item_id, e)
        return False


def _labeled_ids(pool_dir: Path) -> set:
    """池里被人工标注过的 item_id（labels*.jsonl 的 a/b），淘汰时必须保护。

    2026-10-03 事故：容量满后按文件时间淘汰最旧，而被标注的正是最旧那批，
    两天内 216 个已标注视频被清空，模型 usable=0、图表全废。
    """
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


def pool_add(cfg, item_id, src: Path):
    """Copy a downloaded item into the labeling pool (outside the forwarder's
    wipe zone), deduped by item_id, capped at label_pool_max (oldest first).
    Videos go in as .mp4, image posts as their stitched .jpg. Returns True if
    the item is in the pool after the call.

    已标注过的条目永不淘汰（模型训练数据，删了权重就没根了）。"""
    try:
        pool_dir = Path(cfg.get("label_pool_dir") or
                        (Path(cfg["save_root"]).parent / "label_pool"))
        pool_dir.mkdir(parents=True, exist_ok=True)
        dest = pool_dir / f"{item_id}{src.suffix.lower()}"
        if not dest.exists():
            shutil.copy2(src, dest)
            # label_pool_max<=0 = 不限制（2026-10-03 用户要求，D 盘 1TB 用得下）
            cap = int(cfg.get("label_pool_max", 400) or 0)
            if cap > 0:
                files = sorted([*pool_dir.glob("*.mp4"), *pool_dir.glob("*.jpg")],
                               key=lambda p: p.stat().st_mtime)
                if len(files) > cap:
                    protected = _labeled_ids(pool_dir)
                    evictable = [p for p in files if p.stem not in protected]
                    while len(files) > cap and evictable:
                        victim = evictable.pop(0)
                        files.remove(victim)
                        victim.unlink(missing_ok=True)
                        log.info("label pool full (%d), evicted %s", cap, victim.name)
                    if len(files) > cap:
                        log.info("label pool over cap (%d) but all %d candidates "
                                 "are labeled - keeping them", cap, len(files))
        return True
    except Exception as e:
        log.warning("label pool add failed for %s: %s", item_id, e)
        return False


class StatsCsv:
    def __init__(self, root: Path):
        self.root = root

    def append(self, day, row):
        path = self.root / sanitize_day_folder(day) / "stats.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        with open(path, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "sender", "title", "digg", "comment",
                            "collect", "share", "type", "saved", "file"])
            w.writerow(row)


def sanitize_day_folder(day):
    return day


def pick_play_addr(addrs):
    """Prefer douyinvod https URLs; skip /aweme/v1/play/ fallbacks."""
    for u in addrs:
        if "douyinvod.com" in u and u.startswith("https"):
            return u
    for u in addrs:
        if u.startswith("https"):
            return u
    return addrs[0] if addrs else None


def is_audio_url(u) -> bool:
    """图文作品的顶层 play_addr 常常是背景音乐 mp3，不能当视频下载。"""
    ul = (u or "").lower()
    return (".mp3" in ul) or ("ies-music" in ul) or ("/music/" in ul)


def nested_video_url(im: dict):
    """动态照片/带视频的图文：取图片项里嵌的那段视频地址，没有返回 None。

    实测（2026-10-01，作品 7690205655533476198 "阿桥"）：动态照片的
    images[0] 只有静态图，动态部分在 images[0].video.bit_rate[].play_addr，
    顶层 video.play_addr 反而是背景音乐 mp3。
    """
    v = im.get("video") or {}
    for br in v.get("bit_rate") or []:
        addrs = ((br.get("play_addr") or {}).get("url_list")) or []
        u = pick_play_addr([a for a in addrs if not is_audio_url(a)])
        if u:
            return u
    addrs = ((v.get("play_addr") or {}).get("url_list")) or []
    return pick_play_addr([a for a in addrs if not is_audio_url(a)])


def normalize_aweme(a):
    """Full aweme JSON -> the same dict shape _fetch_detail_js returns."""
    v = a.get("video") or {}
    images = []
    image_videos = []
    for im in a.get("images") or []:
        urls = im.get("url_list") or []
        if urls:
            images.append(urls[0])
            image_videos.append(nested_video_url(im) or "")
    return {
        "ok": True,
        "desc": a.get("desc") or "",
        "create_time": a.get("create_time") or 0,
        "is_aigc_media": 1 if a.get("is_aigc_media") else 0,
        "statistics": a.get("statistics") or {},
        "play_addrs": ((v.get("play_addr") or {}).get("url_list")) or [],
        "duration": v.get("duration") or 0,
        "images": images,
        "image_videos": image_videos,
        "music": (((a.get("music") or {}).get("play_url") or {}).get("url_list")) or [],
    }


class MitmFeed:
    """Passive stats source: tails the mitmproxy capture for detail payloads.

    The chat page itself fires /aweme/v1/web/multi/aweme/detail/ when it
    renders a share card; the mitm addon dumps those responses into
    api_YYYYMMDD.jsonl. Reading them costs zero extra API calls, which keeps
    the risk-control footprint minimal. Byte-exact incremental reading; the
    last partial line is left for the next poll.
    """

    def __init__(self, log_dir):
        self.log_dir = Path(log_dir)
        self.index = {}
        self.offsets = {}

    def poll(self):
        for dayoff in (0, 1):
            day = time.strftime("%Y%m%d", time.localtime(time.time() - dayoff * 86400))
            path = self.log_dir / f"api_{day}.jsonl"
            try:
                size = path.stat().st_size
            except OSError:
                continue
            off = self.offsets.get(path, 0)
            if size < off:
                off = 0
            if size == off:
                continue
            try:
                with open(path, "rb") as f:
                    f.seek(off)
                    chunk = f.read()
            except OSError:
                continue
            nl = chunk.rfind(b"\n")
            if nl < 0:
                continue
            self.offsets[path] = off + nl + 1
            for raw in chunk[:nl].split(b"\n"):
                self._ingest_line(raw)

    def _ingest_line(self, raw):
        if b"aweme_detail" not in raw:
            return
        try:
            rec = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return
        body = rec.get("body")
        if not body:
            return
        try:
            payload = json.loads(body)
        except ValueError:
            return
        items = []
        if isinstance(payload.get("aweme_detail"), dict):
            items.append(payload["aweme_detail"])
        for it in payload.get("aweme_details") or []:
            if isinstance(it, dict):
                items.append(it)
        for a in items:
            iid = str(a.get("aweme_id") or "")
            if not iid:
                continue
            # later captures carry fresher stats; never overwrite with empty
            if not a.get("statistics") and iid in self.index:
                continue
            self.index[iid] = normalize_aweme(a)

    def lookup(self, item_id):
        self.poll()
        return self.index.get(str(item_id))


def process_item(browser, tab, card, cfg, manifest, stats_csv, state):
    item_id = card["itemId"]
    detail = None
    feed = state.get("mitm_feed")
    if feed is not None:
        detail = feed.lookup(item_id)
        if not detail and not state.get("passive_only"):
            # the page's own card-detail call can lag a few seconds behind
            # the DOM; give it a short window before burning an API call
            deadline = time.time() + float(cfg.get("mitm_wait_seconds", 15))
            while time.time() < deadline:
                time.sleep(3)
                detail = feed.lookup(item_id)
                if detail:
                    break
    if detail:
        state["consec_fail"] = 0
        state["passive_hits"] = state.get("passive_hits", 0) + 1
    elif state.get("passive_only"):
        # api muted and no captured payload yet — leave the card for a later
        # pass instead of burning an api call or a failure mark
        return False
    else:
        state["active_calls"] = state.get("active_calls", 0) + 1
        try:
            detail = browser.fetch_detail(item_id)
        except Exception as e:
            log.error("detail api error for %s: %s", item_id, e)
            state["consec_fail"] += 1
            time.sleep(min(60, 10 * state["consec_fail"]))
            return False

    if detail.get("fatal"):
        log.error("detail api: %s", detail["fatal"])
        raise RuntimeError(detail["fatal"])
    if detail.get("timeout") or detail.get("jserror"):
        # transport-level problem (likely risk control) — do not burn retries
        log.warning("detail api transport issue for %s: %s", item_id,
                    "timeout" if detail.get("timeout") else detail.get("jserror"))
        state["consec_fail"] += 1
        time.sleep(min(120, 20 * state["consec_fail"]))
        return False
    if detail.get("http") in (403, 412) or detail.get("status_code") not in (None, 0):
        # An isolated 403 usually means the item itself is gone -> count it.
        # A STREAK of 403s means systemic risk control (ArgusSecurity) ->
        # do NOT blame the item (it must stay retryable) and end the pass.
        state["consec_fail"] += 1
        systemic = state["consec_fail"] >= 3
        log.warning("detail api blocked/absent for %s: http=%s status_code=%s%s",
                    item_id, detail.get("http"), detail.get("status_code"),
                    " (systemic block suspected)" if systemic else "")
        if systemic:
            state["systemic"] = True
            global _API_MUTED_UNTIL
            _API_MUTED_UNTIL = time.time() + API_MUTE_SECONDS
            log.warning("api muted for %ds to let the rate limit cool off",
                        API_MUTE_SECONDS)
        else:
            manifest.record(card["key"], None, None, failed_inc=1)
        time.sleep(min(120, 20 * state["consec_fail"]))
        return False
    if not detail.get("ok"):
        log.info("no detail for %s (%s), skipping", item_id,
                 detail.get("status_code") or detail.get("http"))
        manifest.record(card["key"], None, None, failed_inc=1)
        return False

    state["consec_fail"] = 0

    stats = detail.get("statistics", {})
    digg = int(stats.get("digg_count", 0) or 0)
    share = int(stats.get("share_count", 0) or 0)
    comment = int(stats.get("comment_count", 0) or 0)
    collect = int(stats.get("collect_count", 0) or 0)
    # curiosity index: share / digg. Download requires digg >= min_digg and
    # ratio > threshold. digg == 0 never divides (downloads are off then anyway).
    min_digg = int(cfg.get("download_min_digg", 10))
    threshold = float(cfg.get("download_ratio_threshold", 0))
    ratio = (share / digg) if digg > 0 else 0.0
    want_download = digg >= min_digg and ratio >= threshold

    day = datetime.now().strftime("%Y%m%d")
    now = datetime.now()
    folder = Path(cfg["save_root"]) / sanitize(tab.group) / day
    images = detail.get("images", [])
    saved_files = []
    kind = "images" if images else "video"

    if want_download:
        if images:
            sub = folder / f"{now.strftime('%H%M%S')}_{sanitize(card['author'])}_{item_id[-8:]}"
            image_videos = detail.get("image_videos") or []
            for i, u in enumerate(images, 1):
                ext = ".jpg"
                mm = re.search(r"\.(jpg|jpeg|png|webp)", u.split("?")[0], re.I)
                if mm:
                    ext = "." + mm.group(1).lower().replace("jpeg", "jpg")
                dest = sub / f"img_{i:02d}{ext}"
                try:
                    # images are exempt from the video truncation guard:
                    # a small webp is a few KB, not a broken download
                    download(u, dest, min_bytes=1024)
                    saved_files.append(str(dest))
                except Exception as e:
                    log.error("image download failed: %s", e)
                # 动态照片：这张图的动态部分在 images[i].video 里，静态图只是封面，
                # 必须把那段视频也存下来（live_XX.mp4），否则只剩一张不动的照片
                ivurl = image_videos[i - 1] if i - 1 < len(image_videos) else None
                if ivurl:
                    try:
                        download(ivurl, sub / f"live_{i:02d}.mp4", min_bytes=20480)
                        log.info("live-photo %s img %d: motion video saved", item_id, i)
                    except Exception as e:
                        log.warning("live-photo video download failed for %s img %d: %s",
                                    item_id, i, e)
            # photo posts carry a music track — keep it so the compilation
            # can play the original audio under the 2s-per-image segment
            music_urls = detail.get("music") or []
            if music_urls and saved_files:
                try:
                    # short music stingers can be well under the video guard too
                    download(music_urls[0], sub / "music.mp3", timeout=30,
                             min_bytes=1024)
                except Exception as e:
                    log.warning("music download failed: %s", e)
            # 混合作品/特效视频：图片之外还有一段真视频（顶层 play_addr）。
            # 例如"抖音特效"作品 images 只有一张封面海报、真正的特效视频在
            # play_addr 里——以前只存图，看起来就是"一张静态照片"。
            # 注意：普通图集的 play_addr 常常是背景音乐 mp3，要跳过。
            vurl = pick_play_addr([a for a in (detail.get("play_addrs") or [])
                                   if not is_audio_url(a)])
            if vurl:
                try:
                    download(vurl, sub / "video.mp4", min_bytes=20480)
                    log.info("mixed post %s: extra video saved", item_id)
                except Exception as e:
                    log.warning("mixed-post video download failed for %s: %s",
                                item_id, e)
        else:
            url = pick_play_addr(detail.get("play_addrs", []))
            if not url:
                log.info("no play addr for %s, skipping download", item_id)
                want_download = False
            else:
                dest = folder / f"{now.strftime('%H%M%S')}_{sanitize(card['author'])}_{item_id[-8:]}.mp4"
                try:
                    download(url, dest)
                    saved_files.append(str(dest))
                except Exception as e:
                    log.error("video download failed: %s", e)
                    want_download = False

    # perceptual-hash dedup: re-uploads have different itemId but identical
    # content; fingerprint the downloaded media and discard duplicates.
    fp = None
    dup_of = None
    if saved_files:
        fp = media_dedup.fingerprint_files(saved_files, kind)
        if fp:
            dup_of = media_dedup.find_dup(fp, manifest.data, exclude_id=item_id)
            if dup_of:
                log.info("dup of %s, discarding downloaded files", dup_of)
                for f in saved_files:
                    Path(f).unlink(missing_ok=True)
                saved_files = []

    # stitch image posts into one tall jpg for forwarding
    if saved_files and kind == "images":
        stitched = media_dedup.stitch_images(
            [Path(f) for f in saved_files],
            Path(saved_files[0]).parent / "stitched.jpg")
        if stitched:
            saved_files.append(str(stitched))
        # 混合作品/动态照片的视频段补进清单（拼接顺序：每张图 2s，
        # 图后面跟它的动态视频；整篇的 video.mp4 排最后）
        base = Path(saved_files[0]).parent
        for lv in sorted(base.glob("live_*.mp4")):
            saved_files.append(str(lv))
        v = base / "video.mp4"
        if v.exists():
            saved_files.append(str(v))

    # label pool: keep a copy of every downloaded item for Bradley-Terry
    # pairwise labeling. The forwarder wipes the work dir after each burst,
    # so the pool lives outside it and survives. Pool files never feed the
    # compilation pipeline. Videos go in whole; image posts as stitched.jpg.
    comments = None
    if saved_files and not dup_of:
        pooled = False
        if kind == "video":
            pooled = pool_add(cfg, item_id, Path(saved_files[0]))
        elif kind == "images":
            # pool image posts as a VIDEO (2s per image + original music,
            # same presentation as the compilation) — a tall stitched jpg
            # is unreadable in the labeling UI
            folder = Path(saved_files[0]).parent
            music = folder / "music.mp3"
            pooled = pool_add_images(cfg, item_id, folder,
                                     music if music.exists() else None,
                                     caption=(detail.get("desc")
                                              or card.get("title")
                                              or card.get("author") or ""))
        if pooled:
            # comments fuel the BT features (@-mentions ≈ shares, curiosity
            # keywords); one extra signed request per pooled item only
            try:
                cres = browser.fetch_comments(item_id)
                if cres.get("ok"):
                    comments = cres.get("comments") or []
            except Exception as e:
                log.warning("comment fetch failed for %s: %s", item_id, e)

    title = detail.get("desc") or card.get("title", "")
    m = re.match(r"^@(.+?)发了一个抖音短视频", title)
    if m:
        # douyin's auto share copy ("@xxx发了一个抖音短视频，…") carries no
        # info beyond the author name — store the clean name instead
        title = card.get("author") or m.group(1)

    record = {
        "item_id": item_id,
        "files": saved_files,
        "downloaded": bool(saved_files),
        "dup_of": dup_of,
        "fp": fp,
        "type": kind,
        "duration_ms": int(detail.get("duration") or 0),
        "create_time": int(detail.get("create_time") or 0),
        "is_aigc_media": int(detail.get("is_aigc_media") or 0),
        "title": title,
        "author": card.get("author", ""),
        "author_id": card.get("uid", ""),
        "sender": card.get("author", ""),
        "stats": {"digg": digg, "comment": comment, "collect": collect, "share": share},
        "ratio": round(ratio, 3),
        "saved_at": now.isoformat(timespec="seconds"),
    }
    if comments is not None:
        record["comments"] = comments
    manifest.record(card["key"], item_id, record)
    stats_csv.append(day, [
        now.strftime("%H:%M:%S"), card.get("author", ""),
        (record["title"] or "")[:60].replace(",", "，"),
        digg, comment, collect, share,
        kind, "yes" if saved_files else "no",
        ";".join(Path(f).name for f in saved_files),
    ])
    total = sum(Path(f).stat().st_size for f in saved_files if Path(f).exists())
    decision = "DUP" if dup_of else ("SAVED" if saved_files else "stats-only")
    nick = str(card.get("author") or "-").replace("[", "(").replace("]", ")")[:30]
    log.info("STATS %s id=%s digg=%s share=%s ratio=%.2f author=[%s] -> %s (%.1f MB)",
             kind, item_id, digg, share, ratio, nick, decision, total / 1e6)
    time.sleep(random.uniform(*cfg["api_cooldown_seconds"]))
    return bool(saved_files)


def backfill_comments(browser, cfg, manifest):
    """Fetch comments for label-pool videos that predate comment capture.

    The comment API only needs an aweme_id, so every pooled video — even ones
    downloaded days ago — can still be backfilled. One signed request per
    video with the usual cooldown; at ~80 pool items this takes a few minutes
    and runs only while nothing new is being saved.
    """
    pool_dir = Path(cfg.get("label_pool_dir") or
                    (Path(cfg["save_root"]).parent / "label_pool"))
    try:
        pool_ids = {p.stem for p in
                    [*pool_dir.glob("*.mp4"), *pool_dir.glob("*.jpg")]}
    except OSError:
        return 0
    todo = [(iid, rec) for iid, rec in manifest.data["videos"].items()
            if iid in pool_ids and "comments" not in rec]
    if not todo:
        return 0
    # a few per pass: the main loop keeps harvesting between batches
    todo = todo[:5]
    log.info("backfilling comments for %d pool video(s) this pass", len(todo))
    done = 0
    for iid, rec in todo:
        try:
            res = browser.fetch_comments(iid)
        except Exception as e:
            log.error("comment backfill error for %s: %s", iid, e)
            continue
        if res.get("ok"):
            rec["comments"] = res.get("comments") or []
            done += 1
        time.sleep(random.uniform(*cfg["api_cooldown_seconds"]))
    manifest.save()
    log.info("comment backfill: %d/%d", done, len(todo))
    return done


def backfill_stats(browser, cfg, manifest):
    todo = [(iid, rec) for iid, rec in manifest.data["videos"].items()
            if rec.get("files") and "stats" not in rec]
    if not todo:
        return 0
    log.info("backfilling stats for %d saved item(s)", len(todo))
    done = 0
    for iid, rec in todo:
        try:
            detail = browser.fetch_detail(iid)
        except Exception as e:
            log.error("backfill api error for %s: %s", iid, e)
            continue
        if detail.get("ok"):
            stats = detail.get("statistics", {})
            rec["stats"] = {
                "digg": stats.get("digg_count", 0),
                "comment": stats.get("comment_count", 0),
                "collect": stats.get("collect_count", 0),
                "share": stats.get("share_count", 0),
            }
            rec["item_id"] = iid
            rec.setdefault("type", "video")
            done += 1
        time.sleep(random.uniform(*cfg["api_cooldown_seconds"]))
    manifest.save()
    log.info("backfilled %d/%d", done, len(todo))
    return done


def harvest(browser, tab, cfg, manifest, stats_csv, mitm_feed=None):
    browser.close_leftover_overlay(tab)

    # latest-only mode: never walk old history, just poll the bottom of
    # the conversation where new messages arrive.
    # no teleports: long jumps mount/unmount masses of video covers and
    # crash the renderer (memory spikes). A dedicated background tab is
    # walked back to the bottom in small PageDown steps by the keeper.
    # inverted list: scrollTop == 0 is the newest end; distance-from-latest
    # is scrollTop itself.
    info = browser.scroll_info(tab)
    if info:
        log.info("poll [%s] at gap=%dpx", tab.group, info["top"])
    saved = 0
    state = {"consec_fail": 0, "mitm_feed": mitm_feed}  # a 403 STREAK means
    # systemic risk control, and must abort the pass instead of hammering
    cards = browser.cards_in_view(tab)
    if time.time() < _API_MUTED_UNTIL:
        # api muted: still process cards whose stats arrived passively via
        # the mitm capture — that costs zero api calls
        state["passive_only"] = True
        new_cards = sum(1 for c in cards
                        if manifest.cover_state(c["key"]) == "new")
        log.info("api muted (%.0fs left), passive-only pass, %d new card(s) pending",
                 _API_MUTED_UNTIL - time.time(), new_cards)
    current_ids = set()
    for card in cards:
        ident = card.get("itemId") or card["key"]
        current_ids.add(ident)
        cs = manifest.cover_state(card["key"])
        # uuid match: a re-share keeps the same aweme itemId, so it is known
        # even if the cover key shifted (cdn url change, re-render, etc.)
        known = cs == "known" or bool(
            card.get("itemId") and card["itemId"] in manifest.data["videos"])
        if cs == "givenup" and not known:
            continue
        if known:
            # a genuine re-share appears as a KNOWN card that was absent from
            # the previous pass; the original card sitting in view must not
            # re-log as DUP merely because it aged past the 10-minute mark
            if tab.have_prev_view and ident not in tab.prev_view_ids:
                log_known_cover(card, manifest)
            continue
        if process_item(browser, tab, card, cfg, manifest, stats_csv, state):
            saved += 1
        if state.get("systemic"):
            log.warning("systemic api block — ending pass early, retry next poll")
            break
        time.sleep(random.uniform(*cfg["scroll_pause_seconds"]))
    tab.prev_view_ids.clear()
    tab.prev_view_ids.update(current_ids)
    tab.have_prev_view = True
    if state.get("passive_hits") or state.get("active_calls"):
        log.info("stats sources: %d passive / %d active",
                 state.get("passive_hits", 0), state.get("active_calls", 0))
    return saved


def bottom_keeper(browser, cfg):
    """This chat list is INVERTED: scrollTop == 0 is the newest end (visual
    bottom), and scrolling into history increases scrollTop. Verified by CDP
    probe: PageDown moves the in-view timestamps BACKWARD (11:41 -> 10:55),
    PageUp moves them FORWARD (10:55 -> 11:56). So:
      1. Fast path: click the app's native 'N new messages' pill when it is
         visible — the app itself jumps to the latest message.
      2. Slow path: one PageUp (~one screen) every ~6s while scrollTop
         >400 (viewport is ~800px; the newest cards must actually be INSIDE
         the viewport for harvest to see them). No raw scrollTop teleports
         (they crash the renderer).
    Plus: crashed-tab recovery (~30s). No periodic reload — a reload makes
    the app restore the cross-device reading anchor (= jump to days-old
    messages), which was the auto-scroll-up bug.

    Stream liveness probe (~every 5 min): the sidebar item shows the group's
    last-message time ('刚刚' / 'N分钟前') no matter which conversation is
    open. If the sidebar says the group is fresh but the tab's message-list
    signature (scrollHeight + last message text) has not changed for 5+ min,
    that tab's live stream is frozen — re-click the group in the sidebar to
    rebind it."""
    ticks = 0
    next_step_at = {}  # per-tab id -> next small-step time
    live_sig = {}      # per-tab id -> (signature, last_change_ts)
    while True:
        ticks += 1
        for tab in list(browser.tabs):
            try:
                now = time.time()
                tid = tab.cdp.page.get("id", "")
                if now >= next_step_at.get(tid, 0):  # every 10-20s: one small step
                    next_step_at[tid] = now + random.uniform(10, 20)
                    r = tab.cdp.js(f"""
                    (function(){{
                      var sc = document.querySelector({json.dumps(SCROLLER_SEL)});
                      if (!sc) return null;
                      var gap = sc.scrollTop;  // inverted list: distance from newest
                      if (gap < 400) return 'ok';
                      var els = document.querySelectorAll('div,span,button');
                      for (var i=0; i<els.length; i++) {{
                        var t = (els[i].textContent||'').trim();
                        if (/^\\d+条新消息$/.test(t) && els[i].children.length === 0) {{
                          var b = els[i].getBoundingClientRect();
                          if (b.width > 0)
                            return ['pill', Math.round(b.left+b.width/2), Math.round(b.top+b.height/2), gap];
                        }}
                      }}
                      sc.tabIndex = -1;
                      sc.focus();
                      return gap;
                    }})()
                    """)
                    if isinstance(r, list) and r and r[0] == "pill":
                        log.info("native new-message pill found [%s] (gap=%d) — clicking",
                                 tab.group, r[3])
                        browser.chat_click(tab, r[1], r[2])
                    elif isinstance(r, int):
                        tab.cdp.send("Input.dispatchKeyEvent", {
                            "type": "rawKeyDown", "key": "PageUp", "code": "PageUp",
                            "windowsVirtualKeyCode": 33, "nativeVirtualKeyCode": 33})
                        tab.cdp.send("Input.dispatchKeyEvent", {
                            "type": "keyUp", "key": "PageUp", "code": "PageUp",
                            "windowsVirtualKeyCode": 33, "nativeVirtualKeyCode": 33})
                if ticks % 15 == 0:  # ~30s: crashed-tab check
                    href = tab.cdp.js("location.href") or ""
                    if "douyin.com/chat" not in str(href):
                        log.warning("chat tab %s at %r — recovering", tid[:6], href)
                        recover_chat_tab(browser, tab, cfg)
                if ticks % 150 == 0 and tab.group:  # ~5min: stream liveness
                    probe = tab.cdp.js(f"""
                    (function(){{
                      var sc = document.querySelector({json.dumps(SCROLLER_SEL)});
                      var sig = '';
                      if (sc) {{
                        var items = sc.querySelectorAll('[class*=MessageItem]');
                        var last = items.length ? items[items.length-1] : null;
                        sig = sc.scrollHeight + '|' + items.length + '|' +
                              (last ? (last.textContent||'').slice(0, 40) : '');
                      }}
                      var sidebarTime = '';
                      var titles = document.querySelectorAll('.conversationConversationItemtitle');
                      for (var i=0; i<titles.length; i++) {{
                        if ((titles[i].textContent||'').trim() !== {json.dumps(tab.group or '')}) continue;
                        var row = titles[i];
                        for (var up=0; up<6 && row; up++) row = row.parentElement;
                        if (row) {{
                          var m = (row.textContent||'').match(/(\\d+分钟前|刚刚|\\d+小时前|昨天)/);
                          if (m) sidebarTime = m[1];
                        }}
                        break;
                      }}
                      return {{sig: sig, sidebarTime: sidebarTime}};
                    }})()
                    """) or {}
                    sig = probe.get("sig") or ""
                    st = probe.get("sidebarTime") or ""
                    prev = live_sig.get(tid)
                    if not sig:
                        continue
                    if prev is None or prev[0] != sig:
                        live_sig[tid] = (sig, now)
                        continue
                    fresh = st == "刚刚" or bool(re.fullmatch(r"[1-4]?\d分钟前", st))
                    idle_for = now - prev[1]
                    if fresh and idle_for > 300:
                        log.warning("stream frozen on tab %s [%s] (sidebar=%s, "
                                    "unchanged %.0fs) — re-clicking group",
                                    tid[:6], tab.group, st, idle_for)
                        tab.ready = False  # force ensure_group to re-click
                        live_sig[tid] = (sig, now)
            except Exception:
                log.warning("keeper error on tab %s [%s] — recovering",
                            tab.cdp.page.get("id", "")[:6], tab.group)
                recover_chat_tab(browser, tab, cfg)
        time.sleep(2)

def recover_chat_tab(browser, tab, cfg):
    """Tab crashed or wedged. Try a quick CDP navigate; if the renderer is
    hung (no answer), close the tab and drop it — the main loop's
    ensure_connected re-discovers/recreates tabs and re-assigns groups."""
    log.warning("recovering chat tab %s [%s]",
                tab.cdp.page.get("id", "")[:6], tab.group)
    new_tab_url = "https://www.douyin.com/chat"
    try:
        try:
            tab.cdp.ws.settimeout(6)
            tab.cdp.send("Page.navigate", {"url": new_tab_url})
            tab.cdp.ws.settimeout(40)
        except Exception:
            log.warning("renderer hung — closing tab, main loop will rebind")
            try:
                urllib.request.urlopen(
                    cfg["debug_url"] + "/json/close/" + tab.cdp.page["id"], timeout=8)
            except Exception:
                pass
            # force full re-discovery: connect() recreates the missing tab
            # and re-assigns groups on the next pass
            browser.tabs = []
            browser.api = None
    except Exception as e:
        log.error("recover failed: %s", e)
    time.sleep(8)
    tab.ready = False


def main():
    ensure_singleton()
    # GBK console cannot encode emoji nicknames; replace instead of crashing
    # the logging call (logging catches it, but the dump spams the console).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    cfg = load_config()
    groups = cfg.get("group_names") or [cfg["group_name"]]
    # 单标签页模式（2026-10-03 用户要求）：一个 douyin 聊天标签页轮流切群采集，
    # 多余的聊天标签页会被关掉；多群覆盖不减，只是每群每轮各访问一次
    single_tab = bool(cfg.get("single_tab", True))
    save_root = Path(cfg["save_root"])
    save_root.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(save_root / "douyin_saver.log", encoding="utf-8")],
    )
    manifest = Manifest(save_root / cfg["manifest_name"])
    load_logged_covers(save_root)
    stats_csvs = {g: StatsCsv(save_root / sanitize(g)) for g in groups}
    browser = Browser(cfg["debug_url"])
    mitm_feed = MitmFeed(cfg.get("mitm_log_dir",
                                 str(BASE_DIR / "data" / "douyin_mitm")))

    log.info("douyin saver v2 started, groups=%r root=%s", groups, save_root)
    import threading
    threading.Thread(target=bottom_keeper, args=(browser, cfg),
                     daemon=True, name="bottom-keeper").start()
    log.info("bottom keeper started (small PageUp steps + pill fast-path + watchdog)")
    pass_no = 0
    consec_fail = 0
    while True:
        pass_no += 1
        try:
            browser.ensure_connected(groups[:1] if single_tab else groups)
            if single_tab and browser.tabs:
                browser.close_extra_tabs()
            saved_total = 0
            # 单标签页模式：一个标签页轮流切 4 个群（每群每轮各采一次）；
            # 多标签页模式：每个标签页固定盯一个群
            if single_tab and browser.tabs:
                visits = [(browser.tabs[0], g) for g in groups]
            else:
                visits = [(t, t.group) for t in list(browser.tabs)]
            for tab, g in visits:
                if not tab or not g:
                    continue
                tab.group = g
                if not browser.ensure_group(tab):
                    log.warning("group %r not available on tab %s, skipping",
                                tab.group, tab.cdp.page.get("id", "")[:6])
                    continue
                time.sleep(2)
                saved_total += harvest(browser, tab, cfg, manifest,
                                       stats_csvs[tab.group], mitm_feed)
            if cfg.get("backfill_stats", True) and saved_total == 0 \
                    and time.time() >= _API_MUTED_UNTIL:
                backfill_stats(browser, cfg, manifest)
                backfill_comments(browser, cfg, manifest)
            log.info("pass %d: saved %d new item(s)", pass_no, saved_total)
            consec_fail = 0
        except KeyboardInterrupt:
            log.info("stopped by user")
            break
        except Exception as e:
            consec_fail += 1
            log.error("pass failed (%d/5): %s", consec_fail, e)
            if consec_fail >= 5:
                browser.tabs = []
                browser.api = None
                consec_fail = 0
        time.sleep(cfg["poll_seconds"])


if __name__ == "__main__":
    main()
