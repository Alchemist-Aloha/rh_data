#!/usr/bin/env python3
"""Drive the fresh-profile Chromium via CDP to fetch the Stooq US daily zip.

Steps: new tab -> /db/h/ (PoW solves itself) -> click the d_us_txt link ->
captcha appears -> dump the captcha image -> wait for the code (stdin) ->
submit -> trigger download -> poll ~/Downloads for the zip.

Usage: python stooq_cdp.py            (interactive, reads CODE from stdin)
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

CDP = "http://127.0.0.1:9333"
DOWNLOADS = os.path.expanduser("~/Downloads")

_id = 0


def call(ws, method, params=None, timeout=30):
    global _id
    _id += 1
    msg = {"id": _id, "method": method, "params": params or {}}
    ws.send(json.dumps(msg).encode())
    while True:
        line = ws.recv()
        data = json.loads(line)
        if data.get("id") == _id:
            if "error" in data:
                raise RuntimeError(f"{method}: {data['error']}")
            return data.get("result", {})
        # skip events


def wait_event(ws, method, timeout=60):
    ws.settimeout(timeout)
    while True:
        try:
            line = ws.recv()
        except Exception:
            return None
        data = json.loads(line)
        if data.get("method") == method:
            return data.get("params", {})
        if data.get("method") == "Page.downloadWillBegin":
            print("DOWNLOAD:", data["params"].get("suggestedFilename"))
        if data.get("method") == "Page.downloadProgress":
            st = data["params"].get("state")
            if st in ("completed", "canceled", "interrupted"):
                print("DOWNLOAD STATE:", st, data["params"].get("receivedBytes", 0))
                return data["params"]


def main() -> int:
    # new tab (PUT required on newer Chrome)
    req = urllib.request.Request(f"{CDP}/json/new?about:blank", method="PUT")
    tabs = json.load(urllib.request.urlopen(req, timeout=10))
    ws_url = tabs["webSocketDebuggerUrl"]
    import websocket  # local import; installed in rh_data venv?

    ws = websocket.create_connection(ws_url, timeout=30)
    call(ws, "Page.enable")
    call(ws, "Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": DOWNLOADS})
    call(ws, "Runtime.enable")

    # 1. open the DB page (PoW runs natively)
    call(ws, "Page.navigate", {"url": "https://stooq.com/db/h/"})
    time.sleep(6)
    res = call(ws, "Runtime.evaluate", {"expression": "document.title", "returnByValue": True})
    print("title:", res["result"].get("value"))
    time.sleep(2)

    # 2. check if captcha already solved on this profile (ap set); if so skip
    #    the click+captcha dance and go straight to download
    res = call(ws, "Runtime.evaluate", {
        "expression": "typeof ap !== 'undefined' && ap ? 'ap-set' : 'not-set'",
        "returnByValue": True,
    })
    if res["result"].get("value") == "ap-set":
        print("captcha already solved on this profile; skipping click+captcha")
        ws.close()
        return 3  # caller: run again with --download-only
    # click the d_us_txt link (first link whose href contains d_us_txt)
    res = call(ws, "Runtime.evaluate", {
        "expression": """
        (() => {
          const a = [...document.querySelectorAll('a')].find(x => (x.href||'').includes('d_us_txt'));
          if (!a) return 'no link';
          a.click();
          return 'clicked ' + a.href;
        })()
        """,
        "returnByValue": True,
    })
    print("click:", res["result"].get("value"))

    # 3. captcha image -> save (poll until the captcha img appears)
    img_b64 = ""
    for _ in range(10):
        time.sleep(1)
        res = call(ws, "Runtime.evaluate", {
            "expression": """
            (async () => {
              const img = document.querySelector('#cpt_cd img');
              if (!img) return null;
              await new Promise(r => { if (img.complete) r(); else img.onload = r; });
              const c = document.createElement('canvas');
              c.width = img.naturalWidth; c.height = img.naturalHeight;
              c.getContext('2d').drawImage(img, 0, 0);
              return c.toDataURL('image/png').split(',')[1];
            })()
            """,
            "awaitPromise": True,
            "returnByValue": True,
        })
        img_b64 = res["result"].get("value") or ""
        if img_b64:
            break
    if not img_b64:
        res = call(ws, "Runtime.evaluate", {
            "expression": "typeof ap !== 'undefined' && ap ? 'ap-set' : 'no-captcha'",
            "returnByValue": True,
        })
        if res["result"].get("value") == "ap-set":
            print("captcha already solved on this profile; skipping")
            ws.close()
            return 3  # caller: proceed to download in a fresh run
        print("no captcha found", file=sys.stderr)
        return 1
    print("captcha img b64len=", len(img_b64))
    with open("/tmp/cpt_cdp.png", "wb") as f:
        f.write(base64.b64decode(img_b64))
    print("saved /tmp/cpt_cdp.png", len(img_b64))
    # upscale for the vision model
    try:
        from PIL import Image as PILImage
        im = PILImage.open("/tmp/cpt_cdp.png").convert("RGB")
        im = im.resize((im.width * 4, im.height * 4), PILImage.LANCZOS)
        im.save("/tmp/cpt_cdp_big.jpg", "JPEG", quality=95)
    except Exception as e:
        print(f"upscale failed: {e}", file=sys.stderr)

    # 4. try codes: auto-read via aux vision model (opencode mimo), else argv/stdin.
    #    Same captcha allows retries, so we try up to 3 candidate readings.
    candidates = sys.argv[1:] if len(sys.argv) > 1 else []
    if not candidates:
        try:
            out = subprocess.run(
                [
                    "opencode", "run", "--model", "opencode-go/mimo-v2.5",
                    "This is a 4-character CAPTCHA (alphanumeric, dark glyphs on red grid "
                    "background). Give THREE best guesses for the 4 characters, one per "
                    "line, most confident first.",
                    "-f", "/tmp/cpt_cdp_big.jpg",
                ],
                capture_output=True, text=True, timeout=240,
            )
            candidates = [ln.strip().lower() for ln in out.stdout.splitlines()
                          if len(ln.strip()) == 4 and ln.strip().isalnum()][:3]
            print("vision candidates:", candidates)
        except Exception as e:
            print(f"vision failed: {e}", file=sys.stderr)
        if not candidates:
            candidates = [input("CODE=").strip().lower()]
    for code in candidates:
        res = call(ws, "Runtime.evaluate", {
            "expression": f"""
            (async () => {{
              const r = await fetch('/q/l/s/?t={code}', {{credentials: 'include'}});
              return await r.text();
            }})()
            """,
            "awaitPromise": True,
            "returnByValue": True,
        })
        print(f"captcha check ({code}):", res["result"].get("value"))
        if res["result"].get("value", "").strip() == "1":
            break
    else:
        print("all codes rejected", file=sys.stderr)
        ws.close()
        return 2

    # 6. trigger download: fetch the zip in-page and save via blob anchor
    #    (avoids navigation abort; server-side quota still applies)
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
    time.sleep(5)
    ws.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
