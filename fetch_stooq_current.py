#!/usr/bin/env python3
"""Download Stooq's latest Current Data daily file and upsert U.S. rows into SQLite."""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse


BASE = "https://stooq.com"
HEADER = [
    "<TICKER>", "<PER>", "<DATE>", "<TIME>", "<OPEN>",
    "<HIGH>", "<LOW>", "<CLOSE>", "<VOL>", "<OPENINT>",
]


def _navigate(context, page, url: str) -> None:
    """Navigate through CDP so Stooq's JavaScript proof-of-work can run.

    Note: no readyState wait here - the PoW page may never reach 'complete'.
    Callers wait for a page-specific selector instead.
    """
    cdp = context.new_cdp_session(page)
    cdp.send("Page.enable")
    cdp.send("Page.navigate", {"url": url})


def _latest_daily_link(page) -> tuple[str, str]:
    links = page.locator("a[href*='db/d/']").evaluate_all(
        "els => els.map(a => ({href: a.getAttribute('href'), text: a.textContent.trim()}))"
    )
    daily: list[tuple[str, str]] = []
    for link in links:
        query = parse_qs(urlparse(urljoin(BASE, link["href"])).query)
        date = query.get("d", [""])[0]
        if query.get("t") == ["d"] and len(date) == 8 and date.isdigit():
            daily.append((date, link["text"]))
    if not daily:
        raise RuntimeError("Stooq Current Data page has no daily download link")
    return max(daily)


def _vision_candidates(captcha: Path) -> list[str]:
    if not shutil.which("opencode"):
        return []
    for _ in range(2):  # retry once on transient failures
        try:
            result = subprocess.run(
                [
                    "opencode", "run", "--model", "opencode-go/mimo-v2.5",
                    "Read this 4-character CAPTCHA. Return up to three guesses, "
                    "one per line, using only letters and numbers.",
                    "-f", str(captcha),
                ],
                capture_output=True,
                text=True,
                timeout=240,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        candidates = [
            line.strip()
            for line in result.stdout.splitlines()
            if len(line.strip()) == 4 and line.strip().isalnum()
        ][:3]
        if candidates:
            return candidates
    return []


def _check_code(page, code: str) -> bool:
    result = page.evaluate(
        """code => fetch('/q/l/s/?t=' + encodeURIComponent(code),
                         {credentials: 'include'}).then(r => r.text())""",
        code,
    )
    return str(result).strip() == "1"


def _authorize(page, link_text: str, supplied_code: str | None, captcha: Path) -> None:
    # first visit can race the PoW handshake: retry the click until the
    # captcha dialog actually renders. No naturalWidth gate: some captcha
    # variants (SVG) never report it; a visible dialog + paint delay is enough.
    image = page.locator("#cpt_cd img")
    for attempt in range(3):
        page.get_by_role("link", name=link_text, exact=True).evaluate("link => link.click()")
        try:
            image.wait_for(state="visible", timeout=15_000)
            time.sleep(3)  # let the captcha image paint
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(3)
    image.screenshot(path=str(captcha))
    print(f"captcha: {captcha}")

    candidates = [supplied_code] if supplied_code else _vision_candidates(captcha)
    for code in filter(None, candidates):
        if _check_code(page, code):
            print("captcha accepted")
            return

    if not sys.stdin.isatty():
        raise RuntimeError(f"CAPTCHA required; rerun with --code after viewing {captcha}")
    while code := input("CAPTCHA code (blank to abort): ").strip():
        if _check_code(page, code):
            print("captcha accepted")
            return
        print("captcha rejected")
    raise RuntimeError("CAPTCHA not solved")


def _start_download(page, link_text: str):
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    try:
        with page.expect_download(timeout=5_000) as pending:
            page.get_by_role("link", name=link_text, exact=True).evaluate("link => link.click()")
        return pending.value
    except PlaywrightTimeoutError:
        authorized = page.get_by_role("link", name="Download file...", exact=True)
        authorized.wait_for(state="visible", timeout=10_000)
        with page.expect_download(timeout=30_000) as pending:
            authorized.evaluate("link => link.click()")
        return pending.value


def _download(profile: Path, raw: Path, supplied_code: str | None, captcha: Path) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Python Playwright is required in the active environment") from exc

    chromium = next(
        (path for name in ("chromium", "chromium-browser", "google-chrome", "chrome")
         if (path := shutil.which(name))),
        None,
    )
    if not chromium:
        raise RuntimeError("Chromium or Chrome was not found")

    profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile),
            executable_path=chromium,
            headless=True,
            accept_downloads=True,
            device_scale_factor=2,
            args=["--no-sandbox", "--disable-gpu"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            _navigate(context, page, f"{BASE}/db/")
            page.wait_for_selector("a[href*='db/d/']", timeout=30_000)
            date, link_text = _latest_daily_link(page)
            print(f"latest daily file: {date}")

            authorized = page.evaluate("typeof ap !== 'undefined' && Boolean(ap)")
            if not authorized:
                _authorize(page, link_text, supplied_code, captcha)
                _navigate(context, page, f"{BASE}/db/")
                page.wait_for_selector("a[href*='db/d/']", timeout=30_000)
                date, link_text = _latest_daily_link(page)

            download = _start_download(page, link_text)
            failure = download.failure()
            if failure:
                raise RuntimeError(f"Stooq download failed: {failure}")
            download.save_as(str(raw))
            return date
        finally:
            context.close()


def _filter_us(source: Path, target: Path) -> tuple[int, set[str]]:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    count = 0
    dates: set[str] = set()
    try:
        with source.open(newline="", encoding="ascii") as src, temporary.open(
            "w", newline="", encoding="ascii"
        ) as dst:
            reader = csv.reader(src)
            header = next(reader, None)
            if header != HEADER:
                raise ValueError(f"unexpected Stooq header: {header!r}")
            writer = csv.writer(dst, lineterminator="\n")
            writer.writerow(header)
            for line_number, row in enumerate(reader, 2):
                if len(row) != len(HEADER):
                    raise ValueError(f"line {line_number}: expected 10 columns, got {len(row)}")
                if not row[0].endswith(".US"):
                    continue
                if row[1] != "D" or len(row[2]) != 8 or not row[2].isdigit():
                    raise ValueError(f"line {line_number}: invalid U.S. daily row")
                writer.writerow(row)
                dates.add(row[2])
                count += 1
        if not count:
            raise ValueError("download contained no .US rows")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return count, dates


_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol  TEXT    NOT NULL,
    market  TEXT    NOT NULL,
    date    INTEGER NOT NULL,
    open    REAL    NOT NULL,
    high    REAL    NOT NULL,
    low     REAL    NOT NULL,
    close   REAL    NOT NULL,
    volume  INTEGER NOT NULL,
    openint INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, market, date)
) WITHOUT ROWID
"""


def _upsert_db(db: Path, source: Path) -> int:
    """Upsert the filtered .US rows from `source` into the bars table."""
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute(_SCHEMA)
    n = 0
    with source.open(newline="", encoding="ascii") as f:
        for row in csv.DictReader(f):
            if not row.get("<TICKER>", "").endswith(".US"):
                continue
            conn.execute(
                """INSERT INTO bars (symbol, market, date, open, high, low, close, volume, openint)
                   VALUES (?, 'us', ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (symbol, market, date) DO UPDATE SET
                     open = excluded.open, high = excluded.high, low = excluded.low,
                     close = excluded.close, volume = excluded.volume, openint = excluded.openint""",
                (
                    row["<TICKER>"][: -len(".US")],
                    int(row["<DATE>"]),
                    float(row["<OPEN>"]),
                    float(row["<HIGH>"]),
                    float(row["<LOW>"]),
                    float(row["<CLOSE>"]),
                    int(float(row["<VOL>"])),
                    int(float(row["<OPENINT>"] or 0)),
                ),
            )
            n += 1
    conn.commit()
    conn.close()
    return n


def _self_test() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source, target = root / "all.txt", root / "us.txt"
        source.write_text(
            ",".join(HEADER) + "\n"
            "AAPL.US,D,20260812,000000,1,2,1,2,3,0\n"
            "^SPX,D,20260812,000000,1,2,1,2,3,0\n"
            "MSFT.US,D,20260812,000000,1,2,1,2,3,0\n",
            encoding="ascii",
        )
        count, dates = _filter_us(source, target)
        rows = list(csv.reader(target.open(newline="", encoding="ascii")))
        assert count == 2 and dates == {"20260812"}
        assert [row[0] for row in rows[1:]] == ["AAPL.US", "MSFT.US"]
    print("self-test ok")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path,
                        default=Path(__file__).resolve().parent.parent / "d_us_txt.sqlite3",
                        help="SQLite db to upsert into (default: ~/QuantTrading/d_us_txt.sqlite3)")
    parser.add_argument("--code", help="current four-character CAPTCHA code")
    parser.add_argument(
        "--profile",
        type=Path,
        default=Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        / "rh-data" / "stooq-current",
        help="persistent Chromium profile for reusing authorization",
    )
    parser.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.self_test:
        _self_test()
        return 0

    captcha = Path(tempfile.gettempdir()) / "stooq_current_captcha.png"
    try:
        with tempfile.TemporaryDirectory(prefix="stooq-current-") as directory:
            raw = Path(directory) / "current.txt"
            date = _download(args.profile, raw, args.code, captcha)
            filtered = Path(directory) / "us.txt"
            count, dates = _filter_us(raw, filtered)
            args.db.parent.mkdir(parents=True, exist_ok=True)
            upserted = _upsert_db(args.db, filtered)
        print(f"saved: {upserted:,} rows -> {args.db} (stooq date {date}; "
              f"dates: {', '.join(sorted(dates))})")
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
