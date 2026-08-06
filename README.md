# rh_data — Robinhood → Stooq d_us_txt-style daily dataset

Fetches daily OHLCV history from the Robinhood API into files that mirror the
layout and row format of `d_us_txt.zip` from Stooq, then keeps them up to
date with a daily incremental script.

## Layout / format

```
rh_data/
  fetch_history.py     # one-time: fetch maximum daily history for every symbol
  update_daily.py      # daily: fetch the newest bars and append them
  rh_common.py         # shared helpers (login, rate limit, formatting)
  data/
    daily/us/<group>/<symbol>.us.txt     # generated dataset
```

Each file matches `d_us_txt.zip` exactly:

```
<TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<VOL>,<OPENINT>
AADR.US,D,20100721,000000,23.1646,23.1646,22.7969,22.7969,45503,0
```

- `<TICKER>` = uppercase symbol + `.US`, `<PER>` = `D`, `<DATE>` = `YYYYMMDD`,
  `<TIME>` = `000000`, `<OPENINT>` = `0`.
- Group folders are copied from `d_us_txt.zip` (e.g. `nasdaq stocks/1`,
  `nyse etfs/1`, `nasdaq etfs`, `nysemkt stocks`), so the tree can be merged
  back into the archive via `--zip-out`.
- Prices are written with 4 decimals, volume as integer shares.

## Setup

1. Run with a Python environment that has `robin_stocks` (3.4.0) and `toml`:
   `.\.venv\Scripts\python.exe` (from the project root).
2. Credentials: `alt_login.py` (SMS-challenge aware) and `login.toml` live in
   this project dir. Copy `login_example.toml` to `login.toml` and fill in your
   username/password (`login.toml` is git-ignored). On first login you may be
   prompted for a one-time SMS code; the token is cached in
   `~/.tokens/robinhood.pickle` for later runs.
3. Download Stooq daily history `d_us_txt.zip` from https://stooq.com/db/h/ and place it in the project root. The scripts read the zip directly (no extraction).

## Fetch maximum history (one-time)

```powershell
cd <project_root>                                              # folder containing rh_data/
.\.venv\Scripts\python.exe rh_data\fetch_history.py --dry-run   # see the plan first
.\.venv\Scripts\python.exe rh_data\fetch_history.py             # full universe (~13k symbols)
```

- Symbols default to everything in `d_us_txt.zip` (same groups/folders).
- Symbols already fetched are **skipped** — rerunning resumes after an
  interruption. Use `--refresh` to overwrite.
- Limit a test run: `--limit 20` or `--symbols AAPL,MSFT` or `--groups "nasdaq etfs"`.
- Point at a different source archive with `--zip-path <archive.zip>` (defaults
  to `d_us_txt.zip` in the project dir, else the sibling folder).

## Daily update (cron / scheduled task)

```powershell
.\.venv\Scripts\python.exe rh_data\update_daily.py
```

- Fetches `span='month'` for existing symbols and appends only bars newer than
  each file's last date (idempotent — no double counting).
- Symbols present in `d_us_txt.zip` but never fetched get full `5year` history.
- Safe to run any time of day; run it once per day after market close for the
  completed session's bar.

## Rate limiting

Both scripts default to **≤ 10 HTTP requests/minute**. Because symbols are batched (50 per request), the full ~13k symbol
universe costs only ~265 requests ≈ 27 minutes, not 13k requests.

To go faster (Robinhood tolerates more), pass `--rate`:

```powershell
.\.venv\Scripts\python.exe rh_data\update_daily.py --rate 30   # ~9 min for full universe
```

## Merging back into d_us_txt.zip

Both scripts accept `--zip-out <path>`: it copies every member of
`d_us_txt.zip` and overlays the `rh_data` files (newer wins), producing one
archive your pipeline can read:

```powershell
.\.venv\Scripts\python.exe rh_data\update_daily.py --zip-out d_us_txt_merged.zip
```

## Important caveats

- **Robinhood only serves ~5 years of daily bars.** `robin_stocks` 3.4.0
  rejects `span='max'` (the API itself caps daily data at 5 years), so
  `span='5year'` is used — the maximum possible. Symbols with longer history
  in `d_us_txt.zip` will have a gap; that data cannot be recovered from
  Robinhood (use Yahoo Finance or the existing zip for pre-2021 history).
- Symbols that no longer exist on Robinhood (delisted, renamed, foreign) return
  no data and are logged as `FAIL` in `fetch_history.log` — the run continues.
- Robinhood daily bars can carry an `interpolated` flag for synthesized days
  (halts etc.); they are kept as-is. Drop them with a filter if you prefer
  strict real data.
- `update_daily.py` re-checks every symbol each run (a halts/delisted symbol
  with no new bar is simply left unchanged).
