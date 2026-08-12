"""Fetch the full daily history for every Robinhood ADR into SQLite.

Writes rows directly into data/adr.sqlite3 (bars table, upsert). No txt files.
The ADR symbol list is enumerated from the Robinhood instruments API
(?type=adr&active=true), paginated through the rate limiter, and saved to the
symbols table in the adr sqlite. Symbols already in the DB are skipped
(--refresh to refetch), so reruns resume after an interruption.

Rate-limited to <=100 HTTP requests/minute by default (--rate to override).

Usage:
    python fetch_adr_history.py                 # all current Robinhood ADRs
    python fetch_adr_history.py --limit 10      # first 10 ADRs (test run)
    python fetch_adr_history.py --symbols BABA,TCEHY
    python fetch_adr_history.py --refresh       # refetch existing symbols
    python fetch_adr_history.py --dry-run       # plan only (no history fetch)
"""

from __future__ import annotations

import argparse
import os
import sys

import rh_common as rc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", help="comma-separated ADR symbols (default: all Robinhood ADRs)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N symbols (0 = no limit)")
    ap.add_argument("--rate", type=int, default=rc.DEFAULT_RATE, help="max HTTP requests per minute")
    ap.add_argument("--batch", type=int, default=rc.DEFAULT_BATCH, help="symbols per HTTP request")
    ap.add_argument("--refresh", action="store_true", help="refetch even if already in the DB")
    ap.add_argument("--dry-run", action="store_true", help="print the plan without fetching history")
    ap.add_argument("--db", default=rc.ADR_DB,
                    help="SQLite db to write (default: data/adr.sqlite3; --db '' disables)")
    ap.add_argument("--log", default=os.path.join(rc.HERE, "fetch_adr_history.log"))
    args = ap.parse_args()

    conn = rc.open_db(rc.resolve_db_path(args.db)) if (args.db and not args.dry_run) else None
    log_fh = open(args.log, "a", encoding="utf-8")
    rc.log(log_fh, f"=== fetch_adr_history start (rate={args.rate}/min, batch={args.batch}) ===")

    stocks = None
    limiter = None

    # --- 1. Build the ADR symbol universe ----------------------------------
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        rc.log(log_fh, f"symbols from --symbols: {len(symbols)}")
    else:
        manifest = rc.read_adr_manifest()
        if args.dry_run and manifest:
            symbols = manifest
            rc.log(log_fh, f"dry-run: using saved manifest ({len(symbols)} symbols)")
        else:
            rc.do_login()
            stocks = rc.import_stocks()
            limiter = rc.RateLimiter(args.rate)
            symbols = rc.fetch_all_adr_symbols(limiter)
            rc.write_adr_manifest(symbols)
            rc.log(log_fh, f"adr universe from Robinhood: {len(symbols)} symbols")

    if not symbols:
        rc.log(log_fh, "no ADR symbols found (run once without --dry-run to build the manifest)")
        return 1

    # --- 2. Worklist: skip symbols already in the DB ------------------------
    db_syms = rc.db_symbols(conn, "adr") if conn is not None else set()
    todo = [s for s in symbols if args.refresh or s not in db_syms]
    skipped = len(symbols) - len(todo)
    if args.limit:
        todo = todo[: args.limit]
    rc.log(log_fh, f"universe={len(symbols)} already_fetched={skipped} to_fetch={len(todo)}")

    if not todo:
        rc.log(log_fh, "nothing to fetch (use --refresh to overwrite existing rows)")
        return 0

    if args.dry_run:
        rc.log(log_fh, "DRY RUN - would fetch:")
        for sym in todo[:20]:
            rc.log(log_fh, f"  {sym:12s} -> data/adr.sqlite3")
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
        batch = rc.fetch_symbol_batch(stocks, chunk, rc.SPAN_FULL, limiter, args.batch)
        b_ok = b_fail = 0
        for sym in chunk:
            rows = rc.bars_to_rows(sym, batch.get(sym) or [])
            if not rows:
                b_fail += 1
                reason = "no data returned" if sym not in batch else "0 usable rows"
                rc.log(log_fh, f"    FAIL {sym}: {reason}")
                continue
            if conn is not None:
                rc.upsert_rows(conn, rows, "adr")
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
