#!/usr/bin/env python3
"""Download-only variant: reuse the fresh-profile session (captcha already
solved -> ap set) and fetch the US daily zip via in-page blob download."""
import base64
import json
import os
import sys
import time
import urllib.request
import websocket

CDP = "http://127.0.0.1:9333"
DOWNLOADS = os.path.expanduser("~/Downloads")
_id = 0


def call(ws, method, params=None):
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
    time.sleep(6)

    res = call(ws, "Runtime.evaluate", {
        "expression": """
        (async () => {
          const r = await fetch('/db/d/?b=d_us_txt', {credentials: 'include'});
          const ct = r.headers.get('Content-Type') || '';
          const cl = r.headers.get('Content-Length');
          if (ct.includes('text') || ct.includes('html')) {
            return 'ERR: ' + ct + ' ' + (await r.text()).slice(0, 80);
          }
          const b = await r.blob();
          const url = URL.createObjectURL(b);
          const a = document.createElement('a');
          a.href = url; a.download = 'd_us_txt.zip';
          document.body.appendChild(a);
          a.click();
          return 'downloading ' + b.size + ' bytes ct=' + ct + ' cl=' + cl;
        })()
        """,
        "awaitPromise": True,
        "returnByValue": True,
    })
    print("download:", res["result"].get("value"))
    time.sleep(8)
    ws.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
