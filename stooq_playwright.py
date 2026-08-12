#!/usr/bin/env python3
"""Stooq US daily zip via Playwright (port of stooq_cdp.py flow).

Steps: /db/h/ (PoW runs natively) -> click d_us_txt link -> captcha ->
screenshot + upscale -> vision candidates (opencode mimo) -> submit ->
click link again -> expect_download -> ~/Downloads/d_us_txt.zip.
"""
import os
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

DOWNLOADS = os.path.expanduser("~/Downloads")
BASE = "https://stooq.com"
PROFILE = "/tmp/stooq_pw_profile"


def _navigate(context, page, url: str, wait_ms: int = 6000) -> None:
    """page.goto is broken under this env (instant 'Timeout Nms exceeded');
    raw CDP Page.navigate works. Wait for load via document.readyState."""
    cdp = context.new_cdp_session(page)
    cdp.send("Page.enable")
    cdp.send("Page.navigate", {"url": url})
    page.wait_for_function(
        "() => document.readyState === 'complete'", timeout=30000
    )


def main() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(
            PROFILE,
            headless=True,
            accept_downloads=True,
            args=["--no-sandbox", "--disable-gpu"],
        )
        page = browser.new_page()
        _navigate(browser, page, f"{BASE}/db/h/")
        # PoW verify runs in-page; wait for the d_us_txt link to appear
        try:
            page.wait_for_selector("a[href*='d_us_txt']", timeout=30000)
        except Exception:
            print("no d_us_txt link (PoW stuck?)", file=sys.stderr)
            return 1

        # captcha already solved on this profile?
        if page.evaluate("typeof ap !== 'undefined' && ap ? true : false"):
            print("captcha already solved on this profile")
        else:
            page.click("a[href*='d_us_txt']")
            # captcha img
            sel = "#cpt_cd img"
            page.wait_for_selector(sel, timeout=30000)
            page.wait_for_function(
                "s => { const i = document.querySelector(s); return i && i.complete && i.naturalWidth > 0 }",
                arg=sel,
                timeout=15000,
            )
            img = page.locator(sel)
            img.screenshot(path="/tmp/cpt_pw.png")
            from PIL import Image as PILImage

            im = PILImage.open("/tmp/cpt_pw.png").convert("RGB")
            im = im.resize((im.width * 4, im.height * 4), PILImage.Resampling.LANCZOS)
            im.save("/tmp/cpt_pw_big.jpg", "JPEG", quality=95)

            candidates = sys.argv[1:] if len(sys.argv) > 1 else []
            if not candidates:
                out = subprocess.run(
                    [
                        "opencode", "run", "--model", "opencode-go/mimo-v2.5",
                        "This is a 4-character CAPTCHA (alphanumeric, dark glyphs on red grid "
                        "background). Give THREE best guesses for the 4 characters, one per "
                        "line, most confident first.",
                        "-f", "/tmp/cpt_pw_big.jpg",
                    ],
                    capture_output=True, text=True, timeout=240,
                )
                candidates = [ln.strip().lower() for ln in out.stdout.splitlines()
                              if len(ln.strip()) == 4 and ln.strip().isalnum()][:3]
                print("vision candidates:", candidates)
            ok = False
            for code in candidates:
                res = page.evaluate(
                    "(c) => fetch('/q/l/s/?t=' + c, {credentials: 'include'}).then(r => r.text())",
                    code,
                )
                print(f"captcha check ({code}):", res)
                if str(res).strip() == "1":
                    ok = True
                    break
            if not ok:
                print("all codes rejected", file=sys.stderr)
                return 2

        # download: click the d_us_txt link with expect_download
        try:
            with page.expect_download(timeout=30000) as dl:
                page.click("a[href*='d_us_txt']")
            path = os.path.join(DOWNLOADS, "d_us_txt.zip")
            dl.value.save_as(path)
            print("downloaded:", path, os.path.getsize(path), "bytes")
        except Exception as e:
            print("download failed:", str(e)[:200], file=sys.stderr)
            body = page.evaluate(
                "() => fetch('/db/d/?b=d_us_txt', {credentials:'include'}).then(r => r.text())"
            )
            print("endpoint body:", body[:100])
            return 1
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
