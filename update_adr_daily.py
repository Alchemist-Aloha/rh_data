"""Daily incremental update for the ADR dataset, straight into data/adr.sqlite3.

Each run:
  1. Re-enumerates the current ADR list from Robinhood (rate-limited pages) and
     saves it to the symbols table in the adr sqlite, so newly listed ADRs are
     picked up.
  2. New ADRs (not yet in the DB): fetches full '5year' history.
  3. Existing ADRs: fetches span='month' and upserts bars newer than each
     symbol's stored last date (idempotent - safe to rerun).
No txt files are read or written.

Rate-limited to <=100 HTTP requests/minute by default (--rate to override).

Usage:
    python update_adr_daily.py
    python update_adr_daily.py --rate 200
    python update_adr_daily.py --dry-run       # plan only (uses saved manifest)
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
    ap.add_argument("--dry-run", action="store_true", help="print the plan without fetching")
    ap.add_argument("--db", default=rc.ADR_DB,
                    help="SQLite db to write (default: data/adr.sqlite3; --db '' disables)")
    ap.add_argument("--log", default=os.path.join(rc.HERE, "update_adr_daily.log"))
    args = ap.parse_args()

    conn = rc.open_db(rc.resolve_db_path(args.db)) if (args.db and not args.dry_run) else None
    log_fh = open(args.log, "a", encoding="utf-8")
    rc.log(log_fh, f"=== update_adr_daily start (rate={args.rate}/min, batch={args.batch}) ===")

    if args.dry_run:
        current = set(rc.read_adr_manifest())
        if not current:
            rc.log(log_fh, "dry-run: no saved manifest; run fetch_adr_history.py once first")
            return 1
        # read-only view of stored symbols
        ro = rc.open_db(rc.resolve_db_path(args.db))
        local = rc.db_symbols(ro, "adr")
        new = sorted(current - local)
        existing = sorted(local & current)
        rc.log(log_fh, f"dry-run: local={len(local)} current={len(current)} "
                       f"to_update={len(existing)} new={len(new)}")
        for sym in existing[:20]:
            rc.log(log_fh, f"  {sym:12s} last={rc.db_last_date(ro, sym, 'adr')}")
        ro.close()
        if len(existing) > 20:
            rc.log(log_fh, f"  ... and {len(existing) - 20} more")
        for sym in new[:10]:
            rc.log(log_fh, f"  {sym:12s} (NEW, full history)")
        if len(new) > 10:
            rc.log(log_fh, f"  ... and {len(new) - 10} more new ADRs")
        return 0

    # --- Real run -----------------------------------------------------------
    rc.do_login()
    stocks = rc.import_stocks()
    limiter = rc.RateLimiter(args.rate)

    current = set(rc.fetch_all_adr_symbols(limiter))
    rc.write_adr_manifest(current)
    rc.log(log_fh, f"current ADR universe: {len(current)} symbols")

    db_syms = rc.db_symbols(conn, "adr") if conn is not None else set()
    new = sorted(current - db_syms)
    existing = sorted(db_syms & current)
    rc.log(log_fh, f"local={len(db_syms)} to_update={len(existing)} new={len(new)}")

    total_added = 0
    failed = 0

    # Pass 1: never-stored ADRs -> full '5year' history
    if new:
        rc.log(log_fh, f"pass 1: full history for {len(new)} new ADRs")
        for i in range(0, len(new), args.batch):
            chunk = new[i : i + args.batch]
            batch = rc.fetch_symbol_batch(stocks, chunk, rc.SPAN_FULL, limiter, args.batch)
            for sym in chunk:
                rows = rc.bars_to_rows(sym, batch.get(sym) or [])
                if not rows:
                    failed += 1
                    rc.log(log_fh, f"    FAIL {sym}: no data")
                    continue
                if conn is not None:
                    rc.upsert_rows(conn, rows, "adr")
                total_added += len(rows)
            rc.log(log_fh, f"  batch {i // args.batch + 1}: {len(chunk)} new ADRs done")

    # Pass 2: existing ADRs -> span='month', upsert bars newer than last date
    if existing:
        rc.log(log_fh, f"pass 2: incremental update for {len(existing)} ADRs")
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
                last = (rc.db_last_date(conn, sym, "adr") or 0) if conn is not None else 0
                new_rows = [r for r in rows if rc.row_date(r) > last] if conn is not None else rows
                if conn is not None:
                    rc.upsert_rows(conn, rows, "adr")
                b_added += len(new_rows)
            total_added += b_added
            failed += b_fail
            rc.log(log_fh, f"  batch {i // args.batch + 1}: {len(chunk)} ADRs checked, "
                           f"{b_added} new bars, {b_fail} failed")

    rc.log(log_fh, f"=== done: new_adrs={len(new)} bars_added={total_added} failed={failed} ===")
    if conn is not None:
        conn.close()
    log_fh.close()
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
