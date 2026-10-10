"""Open the /label page in a new tab on the debug Chrome and screenshot it."""
import base64
import json
import time
import urllib.request

import websocket

DEBUG = "http://127.0.0.1:9223"
OUT = r"C:\GSMPSWechatBot\data\douyin_saved\image_debug\label_page.jpg"

# open a new tab
req = urllib.request.Request(DEBUG + "/json/new?http://127.0.0.1:9998/label",
                             method="PUT")
tab = json.load(urllib.request.urlopen(req, timeout=10))
ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=20,
                                 suppress_origin=True)
ws.send(json.dumps({"id": 0, "method": "Emulation.setDeviceMetricsOverride",
                    "params": {"width": 980, "height": 1250, "deviceScaleFactor": 1,
                               "mobile": False}}))
ws.recv()
time.sleep(3)
ws.send(json.dumps({"id": 1, "method": "Page.captureScreenshot",
                    "params": {"format": "jpeg", "quality": 80}}))
while True:
    msg = json.loads(ws.recv())
    if msg.get("id") == 1:
        with open(OUT, "wb") as f:
            f.write(base64.b64decode(msg["result"]["data"]))
        print("saved")
        break
ws.close()
# close the tab again
urllib.request.urlopen(urllib.request.Request(
    DEBUG + "/json/close/" + tab["id"]), timeout=10).read()
print("tab closed")
