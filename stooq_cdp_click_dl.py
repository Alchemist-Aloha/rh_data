#!/usr/bin/env python3
"""Click the d_us_txt download link in the ap-set session (9333) and capture
the browser's download events directly (Page.downloadWillBegin/Progress).
The link is a download-only control: a plain click never navigates, so the
in-page fetch() approach was hitting the wrong path.
"""
import json
import os
import sys
import time
import urllib.request

import websocket

CDP = "http://127.0.0.1:9333"
DOWNLOADS = os.path.expanduser("~/Downloads")

_id = 0


def call(ws, method, params=None, timeout=60):
    global _id
    _id += 1
    ws.send(json.dumps({"id": _id, "method": method, "params": params or {}}).encode())
    while True:
        d = json.loads(ws.recv())
        if d.get("id") == _id:
            if "error" in d:
                raise RuntimeError(f"{method}: {d['error']}")
            return d.get("result", {})


def main() -> int:
    req = urllib.request.Request(f"{CDP}/json/new?about:blank", method="PUT")
    tab = json.load(urllib.request.urlopen(req, timeout=10))
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=30)
    call(ws, "Page.enable")
    call(ws, "Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": DOWNLOADS})
    call(ws, "Runtime.enable")
    call(ws, "Page.navigate", {"url": "https://stooq.com/db/h/"})
    time.sleep(8)

    r = call(ws, "Runtime.evaluate", {
        "expression": "(() => { const a = [...document.querySelectorAll('a')].find(x => (x.href||'').includes('d_us_txt')); if (!a) return 'no link'; a.click(); return 'clicked ' + a.href; })()",
        "returnByValue": True,
    })
    print("click:", r["result"].get("value"))

    ws.settimeout(120)
    t0 = time.time()
    while time.time() - t0 < 120:
        d = json.loads(ws.recv())
        m = d.get("method")
        if m == "Page.downloadWillBegin":
            p = d["params"]
            print("DOWNLOAD BEGIN:", p.get("suggestedFilename"), "guid:", p.get("guid"))
        if m == "Page.downloadProgress":
            st = d["params"].get("state")
            print("DOWNLOAD PROGRESS:", st, d["params"].get("receivedBytes", 0))
            if st in ("completed", "canceled", "interrupted"):
                return 0 if st == "completed" else 1
        if m == "Page.navigatedWithinDocument":
            print("in-doc navigation:", d["params"].get("url"))
    return 1


if __name__ == "__main__":
    sys.exit(main())
