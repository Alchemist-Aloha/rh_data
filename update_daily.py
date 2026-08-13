"""Daily incremental update: fetch the newest daily bars and store them in SQLite.

For symbols already in data/us.sqlite3: fetches span='month' (covers up to ~1
missed month of sessions) and upserts bars newer than the stored last date.
For symbols in d_us_txt.zip never stored: fetches full '5year' history.
Idempotent - safe to run multiple times, nothing is double-counted.
No txt files are read or written.

Rate-limited to <=100 HTTP requests/minute by default (--rate to override).

Usage:
    python update_daily.py                    # update everything
    python update_daily.py --rate 200         # faster (Robinhood tolerates more)
    python update_daily.py --dry-run          # plan only, no network
    python update_daily.py --zip-path <archive.zip>   # custom source archive
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
    ap.add_argument("--cross-fill-only", action="store_true",
                    help="only gap-fill us.sqlite3 from the robinhood mirror db (no API calls)")
    ap.add_argument("--no-cross-fill", action="store_true",
                    help="skip the automatic robinhood-mirror gap-fill after fetching")
    ap.add_argument("--zip-path", default=rc.ZIP_PATH,
                    help="source archive for symbol discovery (default: project dir, else sibling)")
    ap.add_argument("--db", default=rc.US_DB,
                    help="SQLite db to write (default: data/us.sqlite3; --db '' disables)")
    ap.add_argument("--log", default=os.path.join(rc.HERE, "update_daily.log"))
    args = ap.parse_args()

    os.makedirs(rc.DATA_ROOT, exist_ok=True)
    conn = rc.open_db(rc.resolve_db_path(args.db)) if (args.db and not args.dry_run) else None
    ro = rc.open_db(rc.resolve_db_path(args.db)) if (args.db and args.dry_run) else conn
    log_fh = open(args.log, "a", encoding="utf-8")
    rc.log(log_fh, f"=== update_daily start (rate={args.rate}/min, batch={args.batch}) ===")

    if args.cross_fill_only:
        if conn is None:
            rc.log(log_fh, "cross-fill-only needs --db (default is fine)")
            return 1
        syms, rows = rc.cross_fill_from_mirror(conn, rc.ROBINHOOD_DB)
        rc.log(log_fh, f"cross-fill: {syms} symbols, {rows} rows copied from "
                       f"{rc.ROBINHOOD_DB}")
        conn.close()
        log_fh.close()
        return 0

    # --- 1. Universe: zip symbols + anything already in the DB --------------
    if not os.path.exists(args.zip_path):
        rc.log(log_fh, f"zip path not found: {args.zip_path}")
        return 1
    groups = rc.discover_symbols_from_zip(args.zip_path)
    db_syms = rc.db_symbols(ro, "us") if ro is not None else set()
    for sym in db_syms:
        groups.setdefault(sym, "unknown")
    if not groups:
        rc.log(log_fh, "no symbols found (run fetch_history.py first)")
        return 1
    symbols = sorted(groups)

    # --- 2. Split into never-stored (full history) vs incremental -----------
    missing: list[str] = []
    existing: list[tuple[str, int]] = []
    for sym in symbols:
        if sym not in db_syms:
            missing.append(sym)
        elif ro is not None:
            existing.append((sym, rc.db_last_date(ro, sym, "us") or 0))

    rc.log(log_fh, f"universe={len(symbols)} incremental={len(existing)} missing={len(missing)}")

    if not existing and not missing:
        rc.log(log_fh, "nothing to update")
        return 0

    if args.dry_run:
        rc.log(log_fh, "DRY RUN - would update:")
        for sym, last in existing[:20]:
            rc.log(log_fh, f"  {sym:10s} last={last}")
        if len(existing) > 20:
            rc.log(log_fh, f"  ... and {len(existing) - 20} more incremental symbols")
        for sym in missing[:10]:
            rc.log(log_fh, f"  {sym:10s} (NEW, full history)")
        if len(missing) > 10:
            rc.log(log_fh, f"  ... and {len(missing) - 10} more new symbols")
        total = len(existing) + len(missing)
        n_req = (total + args.batch - 1) // args.batch
        rc.log(log_fh, f"DRY RUN done: {total} symbols at ~{args.rate}/min "
                       f"=> ~{n_req} requests =~ {n_req * 60 // args.rate // 60} min")
        if ro is not None and ro is not conn:
            ro.close()
        return 0

    # --- 3. Fetch + upsert --------------------------------------------------
    rc.do_login()
    stocks = rc.import_stocks()
    limiter = rc.RateLimiter(args.rate)

    total_added = 0
    failed = 0

    # Pass 1: never-stored symbols -> full '5year' history
    if missing:
        rc.log(log_fh, f"pass 1: full history for {len(missing)} new symbols")
        for i in range(0, len(missing), args.batch):
            chunk = missing[i : i + args.batch]
            batch = rc.fetch_symbol_batch(stocks, chunk, rc.SPAN_FULL, limiter, args.batch)
            for sym in chunk:
                rows = rc.bars_to_rows(sym, batch.get(sym) or [])
                if not rows:
                    failed += 1
                    rc.log(log_fh, f"    FAIL {sym}: no data")
                    continue
                if conn is not None:
                    rc.upsert_rows(conn, rows, "us")
                total_added += len(rows)
            rc.log(log_fh, f"  batch {i // args.batch + 1}: {len(chunk)} new symbols done")

    # Pass 2: existing symbols -> span='month', upsert bars newer than last date
    if existing:
        rc.log(log_fh, f"pass 2: incremental update for {len(existing)} symbols")
        for i in range(0, len(existing), args.batch):
            chunk = existing[i : i + args.batch]
            batch = rc.fetch_symbol_batch(stocks, [c[0] for c in chunk], rc.SPAN_UPDATE, limiter, args.batch)
            b_added = b_fail = 0
            for sym, last in chunk:
                bars = batch.get(sym)
                rows = rc.bars_to_rows(sym, bars or [])
                if not rows:
                    if sym not in batch:
                        b_fail += 1
                        rc.log(log_fh, f"    FAIL {sym}: no data returned")
                    continue
                new_rows = [r for r in rows if rc.row_date(r) > last]
                if conn is not None:
                    rc.upsert_rows(conn, rows, "us")
                b_added += len(new_rows)
                if new_rows:
                    rc.log(log_fh, f"    +{len(new_rows):3d} bars {sym} (was last {last})")
            total_added += b_added
            failed += b_fail
            rc.log(log_fh, f"  batch {i // args.batch + 1}: {len(chunk)} symbols checked, "
                           f"{b_added} new bars, {b_fail} failed")

    rc.log(log_fh, f"=== done: bars_added={total_added} failed={failed} ===")

    # --- 5. Gap-fill from the robinhood mirror (cheap, no API) --------------
    if conn is not None and not args.no_cross_fill:
        syms, rows = rc.cross_fill_from_mirror(conn, rc.ROBINHOOD_DB)
        if rows:
            rc.log(log_fh, f"cross-fill: {syms} symbols, {rows} rows copied from "
                           f"{rc.ROBINHOOD_DB}")

    if conn is not None:
        conn.close()
    log_fh.close()
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
