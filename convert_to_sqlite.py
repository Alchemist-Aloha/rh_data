"""Convert the rh_data .us.txt trees into SQLite databases.

By default writes ONE database per market (the recommended layout), so each
daily update script can maintain its own DB:

    data/us.sqlite3        <- pair 1 (fetch_history.py / update_daily.py)
    data/adr.sqlite3       <- pair 2 (fetch_adr_history.py / update_adr_daily.py)
    data/robinhood.sqlite3 <- pair 3 (fetch_robinhood_history.py / update_robinhood_daily.py)

Each DB has a ``bars`` table with primary key (symbol, market, date), so
reruns are idempotent UPSERTs (all git-ignored under data/). Passing
--db <path> instead writes every selected market into that single combined
database.

Schema:
    bars(symbol TEXT, market TEXT, date INTEGER, open, high, low, close REAL,
         volume INTEGER, openint INTEGER, PRIMARY KEY(symbol, market, date))

Usage:
    python convert_to_sqlite.py                       # three per-market DBs
    python convert_to_sqlite.py --markets us,adr      # subset of trees
    python convert_to_sqlite.py --symbols AAPL,MSFT   # only these symbols
    python convert_to_sqlite.py --limit 1000          # first N symbols
    python convert_to_sqlite.py --db combined.sqlite3 # single combined DB
    python convert_to_sqlite.py --dry-run             # plan only
"""

from __future__ import annotations

import argparse
import os
import sys

import rh_common as rc


def iter_market_files(markets: list[str]):
    """Yield (market, symbol, path) for every .us.txt file in the requested trees."""
    for m in markets:
        if m == "us":
            for sym, group in rc.discover_symbols_from_tree().items():
                yield m, sym, rc.out_path(sym, group)
        elif m == "adr":
            for sym in rc.discover_adr_from_tree():
                yield m, sym, rc.adr_path(sym)
        elif m == "robinhood":
            for sym in rc.discover_robinhood_from_tree():
                yield m, sym, rc.robinhood_path(sym)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="", help="single combined DB path (default: one DB per market)")
    ap.add_argument("--markets", default="us,adr,robinhood",
                    help="comma-separated trees to convert: us, adr, robinhood")
    ap.add_argument("--symbols", default="", help="only these symbols (zip-style keys)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N symbols (0 = no limit)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan without writing")
    ap.add_argument("--log", default=os.path.join(rc.HERE, "convert_to_sqlite.log"))
    args = ap.parse_args()

    markets = [m.strip() for m in args.markets.split(",") if m.strip()]
    log_fh = open(args.log, "a", encoding="utf-8")
    rc.log(log_fh, f"=== convert_to_sqlite start (db='{args.db}', markets={markets}) ===")

    # --- 1. Build the worklist ---------------------------------------------
    wanted = {rc.zip_style(s.strip().upper()) for s in args.symbols.split(",")} if args.symbols else None
    files = [item for item in iter_market_files(markets) if wanted is None or item[1] in wanted]
    if args.limit:
        files = files[: args.limit]

    if not files:
        rc.log(log_fh, "no files found (run fetch_*_history.py first, or check --markets)")
        return 1

    # --- 2. Plan / dry-run --------------------------------------------------
    if args.dry_run:
        rc.log(log_fh, "DRY RUN - plan:")
        by_market = {}
        for m, sym, path in files:
            by_market.setdefault(m, []).append((sym, path))
        for m in markets:
            mf = by_market.get(m, [])
            db = rc.resolve_db_path(args.db) if args.db else rc.MARKET_DB[m]
            rc.log(log_fh, f"  [{m:9s}] {len(mf)} files -> {db}")
            for sym, path in mf[:10]:
                rc.log(log_fh, f"      {sym:12s} -> {os.path.relpath(path, rc.HERE)}")
            if len(mf) > 10:
                rc.log(log_fh, f"      ... and {len(mf) - 10} more")
        return 0

    # --- 3. Write -----------------------------------------------------------
    if args.db:
        # combined mode: everything into one database
        db = rc.resolve_db_path(args.db)
        rc.log(log_fh, f"=== combined convert -> {db} ===")
        conn = rc.open_db(db)
        total = 0
        try:
            for market, sym, path in files:
                total += rc.write_file_to_db(conn, path, market, commit=False)
        finally:
            conn.commit()
            conn.close()
        rc.log(log_fh, f"=== done: {len(files)} files, {total} rows -> {db} ===")
    else:
        # default mode: one database per market
        by_market = {}
        for item in files:
            by_market.setdefault(item[0], []).append(item)
        for m in markets:
            mf = by_market.get(m, [])
            if not mf:
                continue
            db = rc.MARKET_DB[m]
            conn = rc.open_db(db)
            total = 0
            try:
                for market, sym, path in mf:
                    total += rc.write_file_to_db(conn, path, market, commit=False)
            finally:
                conn.commit()
                conn.close()
            rc.log(log_fh, f"  [{m:9s}] {len(mf)} files, {total} rows -> {db}")

    log_fh.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
