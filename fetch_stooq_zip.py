#!/usr/bin/env python3
"""Fetch Stooq DB zip from the user's own IP, bypassing the cloud browser.

Flow: PoW challenge -> auth cookie -> captcha image -> user supplies code
(via vision model) -> validate -> download zip. One cookie jar throughout.
"""
import hashlib
import http.cookiejar
import re
import sys
import time
import urllib.parse
import urllib.request

BASE = "https://stooq.com"
B = "d_us_txt"  # US daily
OUT = "d_us_txt.zip"


def solve(c: str, d: int) -> int:
    n = 0
    while not hashlib.sha256((c + str(n)).encode()).hexdigest().startswith("0" * d):
        n += 1
    return n


def main() -> int:
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = [("User-Agent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36")]

    # 1. PoW
    html = op.open(f"{BASE}/db/h/", timeout=30).read().decode()
    m = re.search(r'const c="([^"]+)",d=(\d+)', html)
    if not m:
        print("no PoW challenge", file=sys.stderr)
        return 1
    c, d = m.group(1), int(m.group(2))
    n = solve(c, d)
    req = urllib.request.Request(
        f"{BASE}/__verify",
        data=urllib.parse.urlencode({"c": c, "n": n}).encode(),
        method="POST",
    )
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    print("verify:", op.open(req, timeout=30).read().decode().strip())

    # 2. captcha image
    img = op.open(f"{BASE}/q/l/s/i/?{int(time.time()*1000)}", timeout=30).read()
    with open("cpt.png", "wb") as f:
        f.write(img)
    print(f"captcha saved: {len(img)} bytes")

    # 3. wait for code (passed in)
    code = sys.argv[1] if len(sys.argv) > 1 else ""
    if not code:
        print("CODE=", end="", flush=True)
        code = input().strip().lower()
    r = op.open(f"{BASE}/q/l/s/?t={code}", timeout=30).read().decode()
    print("captcha check:", r)
    if r.strip() != "1":
        print("captcha rejected", file=sys.stderr)
        return 2

    # 4. download
    with op.open(f"{BASE}/db/d/?b={B}", timeout=60) as resp, open(OUT, "wb") as f:
        total = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            total += len(chunk)
            print(f"\r{total/1e6:,.0f} MB", end="", flush=True)
    print(f"\nsaved {OUT} ({total:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
