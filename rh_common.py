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
    (default 10, per the user's constraint).
  * Batches of symbols share ONE HTTP request, so the full ~13k symbol
    universe costs only ~265 requests, not 13k.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import time
import zipfile
from typing import Iterable

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

DEFAULT_RATE = 10      # max HTTP requests per minute (user constraint)
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
    try:
        from alt_login import login as alt_login
    except Exception:
        alt_login = None
    if alt_login is not None:
        if username and password:
            alt_login(username, password)
        else:
            alt_login()  # uses ~/.tokens pickle cache or prompts interactively
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
    """Append rows newer than the file's last DATE (idempotent). Returns count added.

    Rewrites the file preserving existing rows; duplicates and stale rows are dropped.
    """
    existing = read_rows(path)
    last = row_date(existing[-1]) if existing else 0
    by_date = {row_date(r): r for r in existing}
    added = 0
    for r in rows:
        d = row_date(r)
        if d > last and d not in by_date:
            by_date[d] = r
            added += 1
    if added:
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
        got.update(fetch_batch(stocks, all_cands[i : i + batch], span, limiter))
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
