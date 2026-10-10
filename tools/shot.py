"""Quick screenshot of the chat tab."""
import base64
import json
import urllib.request

import websocket

DEBUG = "http://127.0.0.1:9223"

tabs = json.load(urllib.request.urlopen(DEBUG + "/json", timeout=10))
page = next(t for t in tabs if t.get("type") == "page" and "douyin.com/chat" in t.get("url", ""))
ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=20, suppress_origin=True)
ws.send(json.dumps({"id": 1, "method": "Page.captureScreenshot", "params": {"format": "jpeg", "quality": 70}}))
while True:
    msg = json.loads(ws.recv())
    if msg.get("id") == 1:
        with open(r"C:\GSMPSWechatBot\data\douyin_saved\image_debug\check.jpg", "wb") as f:
            f.write(base64.b64decode(msg["result"]["data"]))
        print("saved")
        break
