# rh_data — Robinhood → Stooq style daily dataset

Fetches daily OHLCV history from the Robinhood API into files that mirror the
layout and row format of Stooq's `d_us_txt.zip`, then keeps them up to date
with daily incremental scripts.

## Three script pairs

Each pair has a **one-time backfill** script (`fetch_*_history.py`) and a
**daily incremental** script (`update_*_daily.py`):

| # | Scripts | Universe (source) | Output tree |
|---|---------|-------------------|-------------|
| 1 | `fetch_history.py` + `update_daily.py` | Symbols in `d_us_txt.zip` (Stooq US archive); group folders mirrored from the zip | `data/daily/us/<group>/<sym>.us.txt` |
| 2 | `fetch_adr_history.py` + `update_adr_daily.py` | All Robinhood ADRs (`/instruments/?type=adr&active=true`) | `data/daily/adr/<sym>.us.txt` |
| 3 | `fetch_robinhood_history.py` + `update_robinhood_daily.py` | Every equity instrument on Robinhood (`/instruments/?active=true`) | `data/daily/robinhood/<sym>.us.txt` |

All pairs write the **same d_us_txt row format**, so the three trees can be
consumed or merged uniformly.

## Layout / format

```
rh_data/
  fetch_history.py           # pair 1: backfill the Stooq US zip 
  update_daily.py            # pair 1: daily US update
  fetch_adr_history.py       # pair 2: backfill Robinhood ADRs
  update_adr_daily.py        # pair 2: daily ADR update
  fetch_robinhood_history.py # pair 3: backfill every Robinhood symbol
  update_robinhood_daily.py  # pair 3: daily full-Robinhood update
  rh_common.py               # shared helpers (login, rate limit, formatting)
  data/
    daily/us/<group>/<symbol>.us.txt     # pair 1  (mirrors d_us_txt.zip)
    daily/adr/<symbol>.us.txt            # pair 2
    daily/robinhood/<symbol>.us.txt      # pair 3
```

Each file matches `d_us_txt.zip` exactly:

```
<TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<VOL>,<OPENINT>
AADR.US,D,20100721,000000,23.1646,23.1646,22.7969,22.7969,45503,0
```

- `<TICKER>` = uppercase symbol + `.US`, `<PER>` = `D`, `<DATE>` = `YYYYMMDD`,
  `<TIME>` = `000000`, `<OPENINT>` = `0`.
- Prices are written with 4 decimals, volume as integer shares.
- Ticker dots are normalized to hyphens in filenames (`BRK.B` → `brk-b.us.txt`,
  TICKER `BRK-B.US`), matching the zip convention.

## Setup

1. Run with a Python environment that has `robin_stocks` (3.4.0) and `toml`:
   `.\.venv\Scripts\python.exe` (from the project root).
2. Credentials: `alt_login.py` (SMS-challenge aware) and `login.toml` live in
   this project dir. Copy `login_example.toml` to `login.toml` and fill in your
   username/password (`login.toml` is git-ignored). On first login you may be
   prompted for a one-time SMS code; the token is cached in
   `~/.tokens/robinhood.pickle` for later runs.
3. Only script pair 1 needs the Stooq archive: download `d_us_txt.zip` from
   https://stooq.com/db/h/ and place it in the project root (or pass
   `--zip-path`). The scripts read the zip directly (no extraction).

---

## Pair 1 — US zip mirror (`fetch_history.py` / `update_daily.py`)

Mirrors the `d_us_txt.zip` universe into `data/daily/us/<group>/`, keeping the
zip's group folders (`nasdaq stocks/1`, `nyse etfs/1`, `nasdaq etfs`, ...).

```powershell
# one-time backfill (skips symbols already fetched; resumable)
.\.venv\Scripts\python.exe rh_data\fetch_history.py --dry-run    # plan first
.\.venv\Scripts\python.exe rh_data\fetch_history.py              # full universe (~13k syms)

# daily incremental (appends only bars newer than each file's last date)
.\.venv\Scripts\python.exe rh_data\update_daily.py
```

Options: `--symbols AAPL,MSFT` · `--groups "nasdaq etfs"` · `--limit 20` ·
`--refresh` · `--zip-path <archive.zip>` · `--zip-out merged.zip` (both scripts
accept `--zip-out` to overlay the tree onto a copy of the source archive).

---

## Pair 2 — ADRs (`fetch_adr_history.py` / `update_adr_daily.py`)

Enumerates Robinhood's ADR list (`/instruments/?type=adr&active=true`) into
`data/daily/adr/`. A manifest of discovered symbols is saved to
`data/daily/adr/_adr_symbols.txt`.

```powershell
# one-time backfill
.\.venv\Scripts\python.exe rh_data\fetch_adr_history.py --dry-run   # plan first
.\.venv\Scripts\python.exe rh_data\fetch_adr_history.py

# daily incremental (re-enumerates to pick up newly listed ADRs)
.\.venv\Scripts\python.exe rh_data\update_adr_daily.py
```

Options: `--symbols BABA,TCEHY` · `--limit N` · `--refresh` · `--rate` ·
`--batch`.

---

## Pair 3 — Full Robinhood (`fetch_robinhood_history.py` / `update_robinhood_daily.py`)

Mirrors **every** equity instrument on Robinhood (stocks, ETFs, ADRs, rights,
warrants — anything with a quote, excluding crypto) into
`data/daily/robinhood/`. A manifest is saved to
`data/daily/robinhood/_robinhood_symbols.txt`.

```powershell
# one-time backfill (~13k symbols: ~130+ pages to enumerate + ~265 history requests)
.\.venv\Scripts\python.exe rh_data\fetch_robinhood_history.py --dry-run   # plan first
.\.venv\Scripts\python.exe rh_data\fetch_robinhood_history.py

# daily incremental
.\.venv\Scripts\python.exe rh_data\update_robinhood_daily.py
# faster daily run (no re-enumeration; misses newly listed symbols):
.\.venv\Scripts\python.exe rh_data\update_robinhood_daily.py --skip-enumerate
```

- `update_robinhood_daily.py` re-enumerates the full instrument list by default
  (~130+ pages ≈ 13 min at 10 req/min) so new listings are picked up. Use
  `--skip-enumerate` to update from the saved manifest instead.
- Options: `--symbols AAPL,BRK.B` · `--limit N` · `--refresh` · `--rate` ·
  `--batch`.

---

## SQLite export (fast indexed reads + atomic updates)

By default the `.us.txt` trees are mirrored into **one database per market**
(the recommended layout), so each daily script maintains its own:

```
data/us.sqlite3        <- pair 1 (fetch_history.py / update_daily.py)
data/adr.sqlite3       <- pair 2 (fetch_adr_history.py / update_adr_daily.py)
data/robinhood.sqlite3 <- pair 3 (fetch_robinhood_history.py / update_robinhood_daily.py)
```

Each DB has an indexed `bars` table with a primary key on `(symbol, market,
date)`, so updates are idempotent UPSERTs.

```powershell
# one-time: convert all three trees into their per-market DBs (git-ignored)
.\.venv\Scripts\python.exe rh_data\convert_to_sqlite.py --dry-run    # plan first
.\.venv\Scripts\python.exe rh_data\convert_to_sqlite.py

# convert a subset / a single combined DB
.\.venv\Scripts\python.exe rh_data\convert_to_sqlite.py --markets us,adr
.\.venv\Scripts\python.exe rh_data\convert_to_sqlite.py --db combined.sqlite3
```

Every fetch/update script writes to its market's DB **by default** (no flag
needed), and `--db <path>` overrides the target. `--db ""` disables:

```powershell
.\.venv\Scripts\python.exe rh_data\update_daily.py            # -> data/us.sqlite3
.\.venv\Scripts\python.exe rh_data\update_adr_daily.py        # -> data/adr.sqlite3
.\.venv\Scripts\python.exe rh_data\update_robinhood_daily.py  # -> data/robinhood.sqlite3
.\.venv\Scripts\python.exe rh_data\update_daily.py --db my.sqlite3   # custom path
```

Schema:

```sql
CREATE TABLE bars (
  symbol  TEXT    NOT NULL,   -- zip-style, e.g. 'AAPL', 'BRK-B'
  market  TEXT    NOT NULL,   -- 'us' | 'adr' | 'robinhood'
  date    INTEGER NOT NULL,   -- YYYYMMDD
  open, high, low, close REAL NOT NULL,
  volume INTEGER NOT NULL, openint INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (symbol, market, date)
) WITHOUT ROWID;
```

Reading in Python:

```python
import pandas as pd, sqlite3
df = pd.read_sql("SELECT * FROM bars WHERE symbol='VOO' ORDER BY date",
                 sqlite3.connect("data/us.sqlite3"))
```

## Rate limiting

All six scripts default to **≤ 100 HTTP requests/minute**. Symbols are batched
(50 per history request), so the full ~13k-symbol universe costs only ~265
history requests ≈ 3 minutes, not 13k requests.

To go faster (Robinhood tolerates more), pass `--rate`:

```powershell
.\.venv\Scripts\python.exe rh_data\update_daily.py --rate 200  # ~1.5 min for full universe
```

Note: enumeration pages (instrument list) also count against the budget in
pairs 2 and 3.

## Merging back into d_us_txt.zip

Only **pair 1** has `--zip-out`: it copies every member of the source archive
and overlays the `rh_data` tree (newer wins), producing one archive your
pipeline can read:

```powershell
.\.venv\Scripts\python.exe rh_data\update_daily.py --zip-out d_us_txt_merged.zip
```

## Important caveats

- **Robinhood only serves ~5 years of daily bars.** `robin_stocks` 3.4.0
  rejects `span='max'` (the API itself caps daily data at 5 years), so
  `span='5year'` is used — the maximum possible. Symbols with longer history
  in `d_us_txt.zip` will have a gap; that data cannot be recovered from
  Robinhood (use the existing zip for pre-2021 history).
- Symbols that no longer exist on Robinhood (delisted, renamed, foreign) return
  no data and are logged as `FAIL` in the run's log — the run continues.
- Robinhood daily bars can carry an `interpolated` flag for synthesized days
  (halts etc.); they are kept as-is. Drop them with a filter if you prefer
  strict real data.
- Daily updates re-check every symbol each run (a halted/delisted symbol with
  no new bar is simply left unchanged).
