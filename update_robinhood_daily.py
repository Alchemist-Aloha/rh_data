"""Daily incremental update for the full-Robinhood dataset (data/daily/robinhood/).

Each run (by default):
  1. Re-enumerates the full Robinhood instrument list (rate-limited pages) and
     saves it to _robinhood_symbols.txt, so newly listed symbols are picked up.
     Pass --skip-enumerate to use the saved manifest instead (much faster; ~13
     min saved at 100 req/min, but new listings are missed).
  2. New symbols (not yet local): fetches full '5year' history.
  3. Existing symbols: fetches span='month' and appends bars newer than each
     file's last date (idempotent - safe to rerun, nothing double-counted).

Rate-limited to <=100 HTTP requests/minute by default (--rate to override).
The full ~13k-symbol universe is ~265 history requests (~27 min at 10/min)
plus ~130+ enumeration pages unless --skip-enumerate is used.

Usage:
    python update_robinhood_daily.py
    python update_robinhood_daily.py --skip-enumerate   # use saved manifest
    python update_robinhood_daily.py --rate 200
    python update_robinhood_daily.py --dry-run          # plan only (uses manifest)
"""

from __future__ import annotations

import argparse
import os
import sys

import rh_common as rc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rate", type=int, default=rc.DEFAULT_RATE, help="max HTTP requests per minute")
    ap.add_argument("--batch", type=int, default=rc.DEFAULT_BATCH, help="symbols per HTTP request")
    ap.add_argument("--skip-enumerate", action="store_true",
                    help="use the saved manifest instead of re-enumerating the instrument list")
    ap.add_argument("--dry-run", action="store_true", help="print the plan without fetching")
    ap.add_argument("--db", default=rc.ROBINHOOD_DB,
                    help="SQLite db to also write (default: data/robinhood.sqlite3; --db '' disables)")
    ap.add_argument("--log", default=os.path.join(rc.HERE, "update_robinhood_daily.log"))
    args = ap.parse_args()

    os.makedirs(rc.ROBINHOOD_ROOT, exist_ok=True)
    conn = rc.open_db(rc.resolve_db_path(args.db)) if args.db else None
    log_fh = open(args.log, "a", encoding="utf-8")
    rc.log(log_fh, f"=== update_robinhood_daily start (rate={args.rate}/min, batch={args.batch}, "
                   f"skip_enumerate={args.skip_enumerate}) ===")

    local = set(rc.discover_robinhood_from_tree())

    if args.dry_run:
        current = set(rc.read_robinhood_manifest())
        if not current:
            rc.log(log_fh, "dry-run: no saved manifest; run fetch_robinhood_history.py once first")
            return 1
        new = sorted(current - local)
        existing = sorted(local & current)
        rc.log(log_fh, f"dry-run: local={len(local)} current={len(current)} "
                       f"to_update={len(existing)} new={len(new)}")
        for sym in existing[:20]:
            last = rc.last_date_in_file(rc.robinhood_path(sym))
            rc.log(log_fh, f"  {sym:12s} last={last} -> {os.path.relpath(rc.robinhood_path(sym), rc.HERE)}")
        if len(existing) > 20:
            rc.log(log_fh, f"  ... and {len(existing) - 20} more")
        for sym in new[:10]:
            rc.log(log_fh, f"  {sym:12s} (NEW, full history)")
        if len(new) > 10:
            rc.log(log_fh, f"  ... and {len(new) - 10} more new symbols")
        return 0

    # --- Real run -----------------------------------------------------------
    rc.do_login()
    stocks = rc.import_stocks()
    limiter = rc.RateLimiter(args.rate)

    if args.skip_enumerate:
        current = set(rc.read_robinhood_manifest())
        if not current:
            rc.log(log_fh, "no manifest found and --skip-enumerate given; run once without it")
            return 1
        rc.log(log_fh, f"using saved manifest: {len(current)} symbols")
    else:
        current = set(rc.fetch_all_robinhood_symbols(limiter))
        rc.write_robinhood_manifest(current)
        rc.log(log_fh, f"current robinhood universe: {len(current)} symbols")

    new = sorted(current - local)
    existing = sorted(local & current)
    rc.log(log_fh, f"local={len(local)} to_update={len(existing)} new={len(new)}")

    total_added = 0
    failed = 0

    # Pass 1: never-fetched symbols -> full '5year' history
    if new:
        rc.log(log_fh, f"pass 1: full history for {len(new)} new symbols")
        for i in range(0, len(new), args.batch):
            chunk = new[i : i + args.batch]
            batch = rc.fetch_symbol_batch(stocks, chunk, rc.SPAN_FULL, limiter, args.batch)
            for sym in chunk:
                rows = rc.bars_to_rows(sym, batch.get(sym) or [])
                if not rows:
                    failed += 1
                    rc.log(log_fh, f"    FAIL {sym}: no data")
                    continue
                rc.write_rows(rc.robinhood_path(sym), rows)
                if conn is not None:
                    rc.upsert_rows(conn, rows, "robinhood")
                total_added += len(rows)
            rc.log(log_fh, f"  batch {i // args.batch + 1}: {len(chunk)} new symbols done")

    # Pass 2: existing symbols -> span='month', append bars newer than last date
    if existing:
        rc.log(log_fh, f"pass 2: incremental update for {len(existing)} symbols")
        for i in range(0, len(existing), args.batch):
            chunk = existing[i : i + args.batch]
            batch = rc.fetch_symbol_batch(stocks, chunk, rc.SPAN_UPDATE, limiter, args.batch)
            b_added = b_fail = 0
            for sym in chunk:
                rows = rc.bars_to_rows(sym, batch.get(sym) or [])
                if not rows:
                    if sym not in batch:
                        b_fail += 1
                        rc.log(log_fh, f"    FAIL {sym}: no data returned")
                    continue
                added = rc.append_new_rows(rc.robinhood_path(sym), rows)
                if conn is not None:
                    rc.upsert_rows(conn, rows, "robinhood")
                b_added += added
            total_added += b_added
            failed += b_fail
            rc.log(log_fh, f"  batch {i // args.batch + 1}: {len(chunk)} symbols checked, "
                           f"{b_added} new bars, {b_fail} failed")

    rc.log(log_fh, f"=== done: new_symbols={len(new)} bars_added={total_added} failed={failed} ===")
    if conn is not None:
        conn.close()
    log_fh.close()
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
