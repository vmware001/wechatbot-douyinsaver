"""mitmproxy addon: capture douyin-family API / WebSocket traffic to JSONL.

Output (under C:\\GSMPSWechatBot\\data\\douyin_mitm):
  flows_YYYYMMDD.jsonl  - every douyin-family request, headers only
  api_YYYYMMDD.jsonl    - JSON/API responses, body included (capped)
  ws_YYYYMMDD.jsonl     - websocket messages (text inline, binary base64 capped)
"""
import base64
import json
import os
import threading
import time

BASE = r"C:\GSMPSWechatBot\data\douyin_mitm"
MAX_BODY = 512 * 1024
MAX_WS_BIN = 200 * 1024

_lock = threading.Lock()

API_HINTS = ("/aweme/", "/im/", "message", "detail", "/webcast/", "/user/",
             "/search/", "/feed", "/comment", "/notification")

DETAIL_HINTS = ("aweme/detail", "multi/aweme/detail")


def _cleanup_old_logs():
    """Drop capture files older than 3 days."""
    try:
        cutoff = time.time() - 3 * 86400
        for name in os.listdir(BASE):
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(BASE, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.unlink(path)
            except OSError:
                pass
    except OSError:
        pass


def _day():
    return time.strftime("%Y%m%d")


def _write(kind, obj):
    line = json.dumps(obj, ensure_ascii=False)
    with _lock:
        path = os.path.join(BASE, f"{kind}_{_day()}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


_CDN_HOSTS = ("douyinvod", "douyinpic", "bytetos", "bytecdn", "douyinstatic",
              "douyinstatic", "ixiguavideo")


def _is_cdn(host):
    return any(h in host for h in _CDN_HOSTS)


class DouyinCapture:
    def response(self, flow):
        try:
            host = flow.request.pretty_host.lower()
            path = flow.request.path or ""
            status = flow.response.status_code if flow.response else None
            ctype = ""
            clen = 0
            if flow.response is not None:
                ctype = flow.response.headers.get("content-type", "")[:100]
                try:
                    clen = int(flow.response.headers.get("content-length") or 0)
                except ValueError:
                    clen = 0
            rec = {
                "t": round(time.time(), 3),
                "method": flow.request.method,
                "host": host,
                "path": path[:600],
                "status": status,
                "ctype": ctype,
                "clen": clen,
                "ua": flow.request.headers.get("user-agent", "")[:160],
            }
            is_api = any(h in path for h in API_HINTS)
            if _is_cdn(host):
                _write("flows", rec)
                return
            # body only for detail endpoints (the data we actually consume)
            # and for error responses (forensics); everything else stays
            # headers-only to keep the capture small.
            want_body = (
                any(h in path for h in DETAIL_HINTS)
                or (isinstance(status, int) and status >= 400)
            )
            if want_body and flow.response is not None:
                try:
                    content = flow.response.get_content(strict=False) or b""
                except Exception:
                    content = b""
                if content:
                    rec["body"] = content[:MAX_BODY].decode("utf-8", "replace")
            _write("api" if (is_api or "json" in ctype) else "flows", rec)
        except Exception:
            pass

    def websocket_message(self, flow):
        try:
            msgs = getattr(flow.websocket, "messages", None)
            if not msgs:
                return
            msg = msgs[-1]
            rec = {
                "t": round(time.time(), 3),
                "host": flow.request.pretty_host,
                "path": (flow.request.path or "")[:300],
                "from_client": msg.from_client,
                "type": msg.type,
            }
            if msg.type == "text":
                rec["text"] = (msg.text or "")[:20000]
            else:
                data = msg.content or b""
                rec["blen"] = len(data)
                if 0 < len(data) <= MAX_WS_BIN:
                    rec["b64"] = base64.b64encode(data).decode("ascii")
            _write("ws", rec)
        except Exception:
            pass


addons = [DouyinCapture()]

_cleanup_old_logs()
