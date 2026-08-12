"""Shared helpers for building a d_us_txt.zip-style dataset from Robinhood.

Mirrors the d_us_txt.zip layout and row format exactly:

    Layout:  data/daily/us/<group>/<symbol>.us.txt
    Header:  <TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<VOL>,<OPENINT>
    Row:     AADR.US,D,20100721,000000,23.1646,23.1646,22.7969,22.7969,45503,0

Notes:
  * robin_stocks 3.4.0 rejects span='max'; the chart API itself caps daily
    bars at 5 years, so SPAN_FULL = '5year' is the maximum daily history
    Robinhood can serve.
  * Requests are rate-limited to <= ``rate`` per 60-second rolling window
    (default 100).
  * Batches of symbols share ONE HTTP request, so the full ~13k symbol
    universe costs only ~265 requests, not 13k.
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import time
import zipfile
from typing import Any, Callable, Iterable

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))        # this project's directory
PROJECT_DIR = HERE
SIBLING_DIR = os.path.dirname(HERE)                      # parent folder with sibling projects
ROBIN_DIR = os.path.join(SIBLING_DIR, "robin-stocks")    # optional sibling fallback (alt_login.py, login.toml)


def _resolve_zip_path() -> str:
    """Prefer a local d_us_txt.zip in the project dir, else the sibling folder."""
    for base in (HERE, SIBLING_DIR):
        candidate = os.path.join(base, "d_us_txt.zip")
        if os.path.exists(candidate):
            return candidate
    return os.path.join(SIBLING_DIR, "d_us_txt.zip")


ZIP_PATH = _resolve_zip_path()                            # archive used for symbol discovery
DATA_ROOT = os.path.join(HERE, "data")                   # generated dataset

HEADER = "<TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<VOL>,<OPENINT>"

DEFAULT_RATE = 100      # max HTTP requests per minute (user constraint)
DEFAULT_BATCH = 50     # symbols per historicals HTTP request
INTERVAL = "day"
SPAN_FULL = "5year"    # max daily history the API serves
SPAN_UPDATE = "month"  # enough to cover ~1 missed month for the daily updater


# ---------------------------------------------------------------------------
# Robinhood setup
# ---------------------------------------------------------------------------

def add_login_to_path() -> None:
    """Make ``alt_login`` importable: project dir first, sibling robin-stocks as fallback."""
    for path in (PROJECT_DIR, ROBIN_DIR):
        if path not in sys.path:
            sys.path.insert(0, path)


def import_stocks():
    add_login_to_path()
    import robin_stocks.robinhood.stocks as stocks
    return stocks


def load_credentials() -> tuple[str, str]:
    """Return (username, password) from login.toml (project dir, then sibling robin-stocks)."""
    try:
        import toml
    except ImportError:
        return "", ""
    for path in (os.path.join(PROJECT_DIR, "login.toml"), os.path.join(ROBIN_DIR, "login.toml")):
        if os.path.exists(path):
            try:
                data = toml.load(path)
                username = str(data.get("username", "") or "")
                password = str(data.get("password", "") or "")
                if username and password:
                    return username, password
            except Exception:
                continue
    return "", ""


def do_login() -> None:
    """Log in once using the project-dir ``alt_login`` (SMS-challenge aware)."""
    add_login_to_path()
    username, password = load_credentials()
    alt_login_func: Callable[..., Any] | None = None
    try:
        from alt_login import login as alt_login_func
    except Exception:
        pass  # alt_login.py missing -> fall back to robin_stocks authentication below
    if alt_login_func is not None:
        if username and password:
            alt_login_func(username, password)
        else:
            alt_login_func()  # uses ~/.tokens pickle cache or prompts interactively
        return
    import robin_stocks.robinhood.authentication as auth
    if username and password:
        auth.login(username, password)
    else:
        auth.login()


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """Token bucket: at most ``rate`` requests per minute, sustained.

    Bursts up to ``rate`` requests are allowed immediately, then one token
    refills every 60/rate seconds, so the long-run rate never exceeds
    ``rate`` requests per 60-second window.
    """

    def __init__(self, rate: int = DEFAULT_RATE) -> None:
        self.rate = max(1, int(rate))
        self._tokens = float(self.rate)
        self._last = time.monotonic()

    def wait(self) -> None:
        """Block until a token is available, then consume it."""
        while True:
            now = time.monotonic()
            self._tokens = min(self.rate, self._tokens + (now - self._last) * self.rate / 60.0)
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            time.sleep((1.0 - self._tokens) * 60.0 / self.rate)


# ---------------------------------------------------------------------------
# Symbol / group discovery
# ---------------------------------------------------------------------------

def zip_style(symbol: str) -> str:
    """Convert a Robinhood-style ticker (dots) to the zip filename style (hyphens).

    e.g. 'BRK.B' -> 'BRK-B', 'AAC-U' -> 'AAC-U'.
    """
    return symbol.replace(".", "-")


def robinhood_candidates(symbol: str) -> list[str]:
    """Robinhood tickers to try for a dataset symbol (dots <-> hyphens).

    The zip normalizes '.' in tickers to '-', so a hyphen may be a share-class
    dot (BRK-B -> BRK.B) or part of the real ticker (AAC-U stays AAC-U). The
    real ticker is tried first; the dot variant is the fallback.
    """
    cands = [symbol]
    if "-" in symbol:
        i = symbol.rfind("-")
        if 0 < i < len(symbol) - 1:
            cands.append(symbol[:i] + "." + symbol[i + 1 :])
    return cands


def discover_symbols_from_zip(zip_path: str = ZIP_PATH) -> dict[str, str]:
    """Return {UPPER_SYMBOL: group_folder} mirroring the d_us_txt.zip layout.

    Zip member layout: data/daily/us/<group>[/1]/<sym>.us.txt  ->  group = 'nasdaq stocks/1'
    """
    out: dict[str, str] = {}
    if not os.path.exists(zip_path):
        return out
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if not name.endswith(".us.txt"):
                continue
            parts = name.split("/")
            if len(parts) < 5 or parts[0] != "data":
                continue
            symbol = parts[-1][: -len(".us.txt")].upper()
            group = "/".join(parts[3:-1])
            out[symbol] = group
    return out


def discover_symbols_from_tree(root: str = DATA_ROOT) -> dict[str, str]:
    """Return {UPPER_SYMBOL: group_folder} from already-written rh_data files."""
    out: dict[str, str] = {}
    base = os.path.join(root, "daily", "us")
    if not os.path.isdir(base):
        return out
    for dirpath, _dirs, files in os.walk(base):
        for fn in files:
            if not fn.endswith(".us.txt"):
                continue
            symbol = fn[: -len(".us.txt")].upper()
            rel = os.path.relpath(dirpath, base).replace("\\", "/")
            out[symbol] = rel
    return out


def out_path(symbol: str, group: str, root: str = DATA_ROOT) -> str:
    """Absolute path where <symbol>'s file belongs (mirrors the zip layout)."""
    return os.path.join(root, "daily", "us", group, f"{symbol.lower()}.us.txt")


# ---------------------------------------------------------------------------
# ADR (American Depositary Receipts) support
# ---------------------------------------------------------------------------

ADR_ROOT = os.path.join(DATA_ROOT, "daily", "adr")       # data/daily/adr


def adr_path(symbol: str, root: str = ADR_ROOT) -> str:
    """Path of an ADR's file inside the adr group (mirrors the zip naming)."""
    return os.path.join(root, f"{zip_style(symbol).lower()}.us.txt")


def discover_adr_from_tree(root: str = ADR_ROOT) -> list[str]:
    """Return ADR symbols already fetched into the adr tree."""
    if not os.path.isdir(root):
        return []
    return sorted(
        fn[: -len(".us.txt")].upper() for fn in os.listdir(root) if fn.endswith(".us.txt")
    )


def read_adr_manifest(root: str = ADR_ROOT) -> list[str]:
    """Read the saved ADR symbol manifest (_adr_symbols.txt), if present."""
    path = os.path.join(root, "_adr_symbols.txt")
    if not os.path.exists(path):
        return []
    return sorted({line.strip().upper() for line in open(path, encoding="utf-8") if line.strip()})


def write_adr_manifest(symbols: Iterable[str], root: str = ADR_ROOT) -> None:
    """Save the ADR symbol list to _adr_symbols.txt (reference only)."""
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "_adr_symbols.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(set(symbols))) + "\n")


def fetch_all_adr_symbols(limiter: RateLimiter, max_pages: int = 0) -> list[str]:
    """Enumerate current ADR instruments from Robinhood (rate-limited).

    Paginates https://api.robinhood.com/instruments/?type=adr&active=true
    through the rate limiter so every page counts against the request budget.
    Filters client-side on type == 'adr' as a safety net in case the server
    ignores the filter. Logs page progress to stderr.
    """
    import robin_stocks.robinhood.helper as helper

    url = "https://api.robinhood.com/instruments/"
    params = {"type": "adr", "active": "true"}
    symbols: list[str] = []
    pages = 0
    while url:
        limiter.wait()
        try:
            res = helper.SESSION.get(url, params=params if pages == 0 else None, timeout=30)
            res.raise_for_status()
            data = res.json()
        except Exception as exc:
            print(f"    !! instruments fetch failed (page {pages + 1}): {exc}", file=sys.stderr)
            break
        pages += 1
        page_adrs = [
            item["symbol"]
            for item in data.get("results", [])
            if item.get("type") == "adr" and item.get("symbol")
        ]
        symbols.extend(page_adrs)
        print(f"    instruments page {pages}: {len(page_adrs)} adrs "
              f"(total {len(symbols)})", file=sys.stderr)
        url = data.get("next")
        if max_pages and pages >= max_pages:
            print(f"    (stopping after {max_pages} pages)", file=sys.stderr)
            break
    return sorted(set(symbols))


# ---------------------------------------------------------------------------
# Full-Robinhood-universe support (all equity instruments, not just ADRs)
# ---------------------------------------------------------------------------

ROBINHOOD_ROOT = os.path.join(DATA_ROOT, "daily", "robinhood")   # data/daily/robinhood


def robinhood_path(symbol: str, root: str = ROBINHOOD_ROOT) -> str:
    """Path of a symbol's file inside the robinhood group (mirrors the zip naming)."""
    return os.path.join(root, f"{zip_style(symbol).lower()}.us.txt")


def discover_robinhood_from_tree(root: str = ROBINHOOD_ROOT) -> list[str]:
    """Return symbols already fetched into the robinhood tree (zip-style keys)."""
    if not os.path.isdir(root):
        return []
    return sorted(
        fn[: -len(".us.txt")].upper() for fn in os.listdir(root) if fn.endswith(".us.txt")
    )


def read_robinhood_manifest(root: str = ROBINHOOD_ROOT) -> list[str]:
    """Read the saved robinhood symbol manifest (_robinhood_symbols.txt)."""
    path = os.path.join(root, "_robinhood_symbols.txt")
    if not os.path.exists(path):
        return []
    return sorted({line.strip().upper() for line in open(path, encoding="utf-8") if line.strip()})


def write_robinhood_manifest(symbols: Iterable[str], root: str = ROBINHOOD_ROOT) -> None:
    """Save the robinhood symbol list to _robinhood_symbols.txt (reference only)."""
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "_robinhood_symbols.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(set(symbols))) + "\n")


def fetch_all_robinhood_symbols(limiter: RateLimiter, max_pages: int = 0) -> list[str]:
    """Enumerate every equity instrument from Robinhood (rate-limited).

    Paginates https://api.robinhood.com/instruments/?active=true through the
    rate limiter so every page counts against the request budget. Returns
    zip-style dataset keys (dots -> hyphens, e.g. BRK.B -> BRK-B). Crypto is
    not part of this endpoint. Logs page progress to stderr.
    """
    import robin_stocks.robinhood.helper as helper

    url = "https://api.robinhood.com/instruments/"
    params = {"active": "true"}
    keys: list[str] = []
    pages = 0
    while url:
        limiter.wait()
        try:
            res = helper.SESSION.get(url, params=params if pages == 0 else None, timeout=30)
            res.raise_for_status()
            data = res.json()
        except Exception as exc:
            print(f"    !! instruments fetch failed (page {pages + 1}): {exc}", file=sys.stderr)
            break
        pages += 1
        page_results = data.get("results", [])
        for item in page_results:
            sym = item.get("symbol")
            if sym:
                keys.append(zip_style(sym))
        print(f"    instruments page {pages}: {len(page_results)} instruments "
              f"(total {len(keys)})", file=sys.stderr)
        url = data.get("next")
        if max_pages and pages >= max_pages:
            print(f"    (stopping after {max_pages} pages)", file=sys.stderr)
            break
    return sorted(set(keys))


# ---------------------------------------------------------------------------
# Row formatting / file IO (d_us_txt format)
# ---------------------------------------------------------------------------

def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def bars_to_rows(symbol: str, bars: Iterable[dict]) -> list[str]:
    """Convert robin_stocks daily historicals bars to d_us_txt rows.

    Rows are sorted ascending by DATE and deduplicated (last bar wins).
    Keys expected per bar: begins_at, open_price, high_price, low_price,
    close_price, volume.
    """
    ticker = f"{symbol.upper()}.US"
    by_date: dict[int, str] = {}
    for bar in bars:
        begins = str(bar.get("begins_at") or "")
        date_s = begins[:10].replace("-", "")
        if len(date_s) != 8 or not date_s.isdigit():
            continue
        open_p = _num(bar.get("open_price"))
        high_p = _num(bar.get("high_price"))
        low_p = _num(bar.get("low_price"))
        close_p = _num(bar.get("close_price"))
        vol = int(_num(bar.get("volume")))
        row = (
            f"{ticker},D,{date_s},000000,"
            f"{open_p:.4f},{high_p:.4f},{low_p:.4f},{close_p:.4f},{vol},0"
        )
        by_date[int(date_s)] = row
    return [by_date[d] for d in sorted(by_date)]


def row_date(row: str) -> int:
    """Extract YYYYMMDD from a data row (column index 2)."""
    parts = row.split(",")
    return int(parts[2]) if len(parts) >= 4 and parts[2].isdigit() else 0


def read_rows(path: str) -> list[str]:
    """Read data rows (skips the header line) from a .us.txt file."""
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        lines = [ln.strip() for ln in f if ln.strip()]
    if lines and lines[0].startswith("<TICKER>"):
        lines = lines[1:]
    return lines


def last_date_in_file(path: str) -> int | None:
    rows = read_rows(path)
    return row_date(rows[-1]) if rows else None


def write_rows(path: str, rows: list[str]) -> None:
    """Write (overwrite) a file with header + rows, ascending by date."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(HEADER + "\n")
        for r in rows:
            f.write(r + "\n")


def append_new_rows(path: str, rows: list[str]) -> int:
    """Append rows newer than the file's last DATE and refresh the last row.

    Idempotent. Returns the number of NEW bars added.

    - Rows with a date strictly newer than the file's last date are appended.
    - The row for the file's LAST date is refreshed when the fetch contains a
      bar for that same date (a completed session's bar is more authoritative
      than anything already on disk, e.g. a mid-day partial-volume snapshot).
    - Duplicate and stale rows are dropped.
    """
    existing = read_rows(path)
    last = row_date(existing[-1]) if existing else 0
    by_date = {row_date(r): r for r in existing}
    added = 0
    refreshed = False
    for r in rows:
        d = row_date(r)
        if d > last and d not in by_date:
            by_date[d] = r
            added += 1
        elif d == last and d and by_date.get(d) != r:
            by_date[d] = r  # replace the most recent bar with the fresh one
            refreshed = True
    if added or refreshed:
        write_rows(path, [by_date[d] for d in sorted(by_date)])
    return added


# ---------------------------------------------------------------------------
# Network fetch (rate limited)
# ---------------------------------------------------------------------------

def fetch_batch(stocks, rh_symbols: list[str], span: str, limiter: RateLimiter) -> dict[str, list[dict]]:
    """Fetch daily historicals for a batch of ROBINHOOD tickers in ONE HTTP request.

    Returns {rh_symbol: [bars]}. Returns {} on network/API failure (caller logs).

    NOTE: robin_stocks 3.4.0's get_stock_historicals returns a FLAT list of bar
    dicts (each tagged with a 'symbol' key), not {symbol, historicals} groups,
    so bars are re-grouped by symbol here.
    """
    if not rh_symbols:
        return {}
    limiter.wait()
    try:
        data = stocks.get_stock_historicals(
            rh_symbols, interval=INTERVAL, span=span, bounds="regular"
        )
    except Exception as exc:
        print(f"    !! request failed ({len(rh_symbols)} syms): {exc}", file=sys.stderr)
        return {}
    out: dict[str, list[dict]] = {}
    for item in data or []:
        if not isinstance(item, dict):
            continue
        sym = item.get("symbol")
        if sym:
            out.setdefault(sym, []).append(item)
    return out


def fetch_symbol_batch(
    stocks, symbols: list[str], span: str, limiter: RateLimiter, batch: int = DEFAULT_BATCH
) -> dict[str, list[dict]]:
    """Fetch a batch of DATASET symbols, trying each symbol's Robinhood candidates.

    Returns {dataset_symbol: [bars]} using the first candidate that returned data.
    Every request is rate-limited; candidate lists are chunked by ``batch``.

    If a whole chunk comes back empty (e.g. the historicals endpoint returns
    HTTP 400 because one ticker in the batch is problematic), the chunk is
    retried symbol-by-symbol so a single bad ticker cannot sink the rest.
    """
    cand_map = {s: robinhood_candidates(s) for s in symbols}
    all_cands: list[str] = []
    seen: set[str] = set()
    for s in symbols:
        for c in cand_map[s]:
            if c not in seen:
                seen.add(c)
                all_cands.append(c)
    got: dict[str, list[dict]] = {}
    for i in range(0, len(all_cands), batch):
        chunk = all_cands[i : i + batch]
        res = fetch_batch(stocks, chunk, span, limiter)
        if not res:
            # whole chunk failed (e.g. HTTP 400) -> isolate the bad tickers
            print(f"    ! batch of {len(chunk)} returned nothing - retrying individually",
                  file=sys.stderr)
            for c in chunk:
                res.update(fetch_batch(stocks, [c], span, limiter))
        got.update(res)
    out: dict[str, list[dict]] = {}
    for s in symbols:
        for c in cand_map[s]:
            if c in got:
                out[s] = got[c]
                break
    return out


# ---------------------------------------------------------------------------
# Optional zip packaging
# ---------------------------------------------------------------------------

def merge_into_zip(src_zip: str, out_zip: str, root: str = DATA_ROOT) -> int:
    """Copy d_us_txt.zip members and overlay rh_data files (newer wins).

    Produces a single archive compatible with the existing pipeline. Returns the
    number of rh_data members overlaid.
    """
    os.makedirs(os.path.dirname(os.path.abspath(out_zip)), exist_ok=True)

    # rh_data members that will replace (not duplicate) source members
    overlay: dict[str, str] = {}  # member name -> local file path
    base = os.path.join(root, "daily", "us")
    if os.path.isdir(base):
        for dirpath, _dirs, files in os.walk(base):
            for fn in files:
                if not fn.endswith(".us.txt"):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, base).replace("\\", "/")
                overlay[f"data/daily/us/{rel}"] = full

    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zout:
        if os.path.exists(src_zip):
            with zipfile.ZipFile(src_zip) as zin:
                for info in zin.infolist():
                    if info.is_dir() or info.filename in overlay:
                        continue
                    zout.writestr(info, zin.read(info.filename))
        for member, full in overlay.items():
            zout.write(full, member)
    return len(overlay)


# ---------------------------------------------------------------------------
# SQLite export
# ---------------------------------------------------------------------------

# One database per market (the default layout); each pair's scripts default
# to its own DB. Passing --db <path> anywhere writes to a single combined DB.
US_DB = os.path.join(DATA_ROOT, "us.sqlite3")
ADR_DB = os.path.join(DATA_ROOT, "adr.sqlite3")
ROBINHOOD_DB = os.path.join(DATA_ROOT, "robinhood.sqlite3")
MARKET_DB = {"us": US_DB, "adr": ADR_DB, "robinhood": ROBINHOOD_DB}

_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol  TEXT    NOT NULL,   -- zip-style symbol, e.g. 'AAPL', 'BRK-B'
    market  TEXT    NOT NULL,   -- 'us' | 'adr' | 'robinhood'
    date    INTEGER NOT NULL,   -- YYYYMMDD
    open    REAL    NOT NULL,
    high    REAL    NOT NULL,
    low     REAL    NOT NULL,
    close   REAL    NOT NULL,
    volume  INTEGER NOT NULL,
    openint INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, market, date)
) WITHOUT ROWID
"""


def resolve_db_path(db_arg: str) -> str:
    """Resolve a --db argument to an absolute path (relative -> project dir)."""
    return db_arg if os.path.isabs(db_arg) else os.path.join(HERE, db_arg)


def open_db(db_path: str = US_DB) -> sqlite3.Connection:
    """Open (creating if needed) the bars database and ensure the schema."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(_DB_SCHEMA)
    conn.commit()
    return conn


def rows_to_db_rows(rows: Iterable[str], market: str) -> list[tuple]:
    """Convert d_us_txt rows to (symbol, market, date, o, h, l, c, vol, openint)."""
    out: list[tuple] = []
    for row in rows:
        p = row.split(",")
        if len(p) < 9:
            continue
        symbol = p[0]
        for suffix in (".US", ".us"):
            if symbol.endswith(suffix):
                symbol = symbol[: -len(suffix)]
                break
        try:
            out.append((
                symbol,
                market,
                int(p[2]),
                float(p[4]), float(p[5]), float(p[6]), float(p[7]),
                int(float(p[8])),
                int(float(p[9])) if len(p) > 9 and p[9] else 0,
            ))
        except ValueError:
            continue
    return out


def upsert_rows(conn: sqlite3.Connection, rows: Iterable[str], market: str, commit: bool = True) -> int:
    """Upsert d_us_txt rows into the bars table (idempotent). Returns row count."""
    data = rows_to_db_rows(rows, market)
    if not data:
        return 0
    conn.executemany(
        """INSERT INTO bars (symbol, market, date, open, high, low, close, volume, openint)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (symbol, market, date) DO UPDATE SET
             open = excluded.open, high = excluded.high, low = excluded.low,
             close = excluded.close, volume = excluded.volume, openint = excluded.openint""",
        data,
    )
    if commit:
        conn.commit()
    return len(data)


def write_file_to_db(conn: sqlite3.Connection, path: str, market: str, commit: bool = True) -> int:
    """Upsert one .us.txt file's rows into the bars table. Returns row count."""
    return upsert_rows(conn, read_rows(path), market, commit=commit)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(stream, msg: str) -> None:
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    if stream is not None:
        try:
            stream.write(line + "\n")
            stream.flush()
        except Exception:
            pass
