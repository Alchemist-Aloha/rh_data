"""Fetch the full daily history for EVERY equity symbol from Robinhood.

This is the complete-Robinhood mirror: it enumerates all instruments from
https://api.robinhood.com/instruments/?active=true (stocks, ETFs, ADRs,
rights, warrants - anything with a quote, excluding crypto) and writes
d_us_txt-style files to a dedicated tree:

    rh_data/data/daily/robinhood/<symbol>.us.txt   e.g. aapl.us.txt -> AAPL.US,D,...
    rh_data/data/daily/robinhood/_robinhood_symbols.txt   manifest

The symbol list is paginated through the rate limiter (every page counts
against the request budget; the full ~13k-instrument list is ~130+ pages).
Symbols already fetched are skipped (--refresh to overwrite), so reruns
resume after an interruption.

Rate-limited to <=100 HTTP requests/minute by default (--rate to override).

Usage:
    python fetch_robinhood_history.py               # every Robinhood symbol
    python fetch_robinhood_history.py --limit 20    # first 20 symbols (test run)
    python fetch_robinhood_history.py --symbols AAPL,BRK.B
    python fetch_robinhood_history.py --refresh     # overwrite existing files
    python fetch_robinhood_history.py --dry-run     # plan only (uses manifest)
"""

from __future__ import annotations

import argparse
import os
import sys

import rh_common as rc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", help="comma-separated symbols (default: all Robinhood instruments)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N symbols (0 = no limit)")
    ap.add_argument("--rate", type=int, default=rc.DEFAULT_RATE, help="max HTTP requests per minute")
    ap.add_argument("--batch", type=int, default=rc.DEFAULT_BATCH, help="symbols per HTTP request")
    ap.add_argument("--refresh", action="store_true", help="refetch even if the file already exists")
    ap.add_argument("--dry-run", action="store_true", help="print the plan without fetching history")
    ap.add_argument("--db", default=rc.ROBINHOOD_DB,
                    help="SQLite db to also write (default: data/robinhood.sqlite3; --db '' disables)")
    ap.add_argument("--log", default=os.path.join(rc.HERE, "fetch_robinhood_history.log"))
    args = ap.parse_args()

    os.makedirs(rc.ROBINHOOD_ROOT, exist_ok=True)
    conn = rc.open_db(rc.resolve_db_path(args.db)) if (args.db and not args.dry_run) else None
    log_fh = open(args.log, "a", encoding="utf-8")
    rc.log(log_fh, f"=== fetch_robinhood_history start (rate={args.rate}/min, batch={args.batch}) ===")

    stocks = None
    limiter = None

    # --- 1. Build the symbol universe (zip-style keys) ---------------------
    if args.symbols:
        # normalize dots to the zip's hyphen style (BRK.B -> BRK-B)
        symbols = [rc.zip_style(s.strip().upper()) for s in args.symbols.split(",") if s.strip()]
        rc.log(log_fh, f"symbols from --symbols: {len(symbols)}")
    else:
        manifest = rc.read_robinhood_manifest()
        if args.dry_run and manifest:
            symbols = manifest
            rc.log(log_fh, f"dry-run: using saved manifest ({len(symbols)} symbols)")
        else:
            rc.do_login()
            stocks = rc.import_stocks()
            limiter = rc.RateLimiter(args.rate)
            symbols = rc.fetch_all_robinhood_symbols(limiter)
            rc.write_robinhood_manifest(symbols)
            rc.log(log_fh, f"robinhood universe: {len(symbols)} symbols")

    if not symbols:
        rc.log(log_fh, "no symbols found (run once without --dry-run to build the manifest)")
        return 1

    # --- 2. Worklist: skip files that already exist ------------------------
    todo: list[tuple[str, str]] = []
    skipped = 0
    for sym in symbols:
        path = rc.robinhood_path(sym)
        if not args.refresh and os.path.exists(path) and rc.read_rows(path):
            skipped += 1
            continue
        todo.append((sym, path))
    if args.limit:
        todo = todo[: args.limit]
    rc.log(log_fh, f"universe={len(symbols)} already_fetched={skipped} to_fetch={len(todo)}")

    if not todo:
        rc.log(log_fh, "nothing to fetch (use --refresh to overwrite existing files)")
        return 0

    if args.dry_run:
        rc.log(log_fh, "DRY RUN - would fetch:")
        for sym, path in todo[:20]:
            rc.log(log_fh, f"  {sym:12s} -> {os.path.relpath(path, rc.HERE)}")
        if len(todo) > 20:
            rc.log(log_fh, f"  ... and {len(todo) - 20} more")
        n_req = (len(todo) + args.batch - 1) // args.batch
        rc.log(log_fh, f"DRY RUN done: {len(todo)} symbols at ~{args.rate}/min "
                       f"=> ~{n_req} requests =~ {n_req * 60 // args.rate // 60} min")
        return 0

    # --- 3. Fetch in rate-limited batches ----------------------------------
    if stocks is None:  # --symbols path skipped login/enumeration
        rc.do_login()
        stocks = rc.import_stocks()
        limiter = rc.RateLimiter(args.rate)
    assert limiter is not None  # guaranteed by the block above

    ok = failed = 0
    n_batches = (len(todo) + args.batch - 1) // args.batch
    for i in range(0, len(todo), args.batch):
        chunk = todo[i : i + args.batch]
        batch = rc.fetch_symbol_batch(stocks, [c[0] for c in chunk], rc.SPAN_FULL, limiter, args.batch)
        b_ok = b_fail = 0
        for sym, path in chunk:
            rows = rc.bars_to_rows(sym, batch.get(sym) or [])
            if not rows:
                b_fail += 1
                reason = "no data returned" if sym not in batch else "0 usable rows"
                rc.log(log_fh, f"    FAIL {sym}: {reason}")
                continue
            rc.write_rows(path, rows)
            if conn is not None:
                rc.upsert_rows(conn, rows, "robinhood")
            b_ok += 1
        ok += b_ok
        failed += b_fail
        rc.log(log_fh, f"  batch {i // args.batch + 1}/{n_batches} ({len(chunk)} syms): "
                       f"ok={b_ok} fail={b_fail}")

    rc.log(log_fh, f"=== done: ok={ok} failed={failed} (see failed symbols above) ===")
    if conn is not None:
        conn.close()
    log_fh.close()
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
