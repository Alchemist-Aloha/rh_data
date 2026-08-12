"""Fetch the maximum daily history for every symbol from Robinhood into SQLite.

Writes rows directly into data/us.sqlite3 (bars table, upsert). No txt files.
Symbols already present in the DB are skipped (--refresh to refetch), so the
script is resumable after an interrupted run.

Usage:
    python fetch_history.py                    # full universe from d_us_txt.zip
    python fetch_history.py --limit 20         # first 20 symbols (test run)
    python fetch_history.py --symbols AAPL,MSFT
    python fetch_history.py --groups "nasdaq etfs,nysemkt stocks"
    python fetch_history.py --zip-path <archive.zip>   # custom source archive
    python fetch_history.py --refresh          # refetch existing symbols
    python fetch_history.py --dry-run          # plan only, no network
"""

from __future__ import annotations

import argparse
import os
import sys

import rh_common as rc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", help="comma-separated symbols (default: all in d_us_txt.zip)")
    ap.add_argument("--groups", help="comma-separated group filter, e.g. 'nasdaq etfs,nysemkt stocks'")
    ap.add_argument("--limit", type=int, default=0, help="stop after N symbols (0 = no limit)")
    ap.add_argument("--rate", type=int, default=rc.DEFAULT_RATE, help="max HTTP requests per minute")
    ap.add_argument("--batch", type=int, default=rc.DEFAULT_BATCH, help="symbols per HTTP request")
    ap.add_argument("--refresh", action="store_true", help="refetch even if already in the DB")
    ap.add_argument("--dry-run", action="store_true", help="print the plan without fetching")
    ap.add_argument("--zip-path", default=rc.ZIP_PATH,
                    help="source archive for symbol discovery (default: project dir, else sibling)")
    ap.add_argument("--db", default=rc.US_DB,
                    help="SQLite db to write (default: data/us.sqlite3; --db '' disables)")
    ap.add_argument("--log", default=os.path.join(rc.HERE, "fetch_history.log"))
    args = ap.parse_args()

    os.makedirs(rc.DATA_ROOT, exist_ok=True)
    conn = rc.open_db(rc.resolve_db_path(args.db)) if (args.db and not args.dry_run) else None
    log_fh = open(args.log, "a", encoding="utf-8")
    rc.log(log_fh, f"=== fetch_history start (rate={args.rate}/min, batch={args.batch}) ===")

    # --- 1. Build the symbol -> group universe -----------------------------
    if not os.path.exists(args.zip_path):
        rc.log(log_fh, f"zip path not found: {args.zip_path}")
        return 1
    if args.symbols:
        # normalize dots to the zip's hyphen style (BRK.B -> BRK-B)
        explicit = [rc.zip_style(s.strip().upper()) for s in args.symbols.split(",") if s.strip()]
        zip_groups = rc.discover_symbols_from_zip(args.zip_path)
        groups = {sym: zip_groups.get(sym, "custom") for sym in explicit}  # select only these
    else:
        groups = rc.discover_symbols_from_zip(args.zip_path)
        if args.groups:
            wanted = {g.strip().lower() for g in args.groups.split(",") if g.strip()}
            groups = {s: g for s, g in groups.items() if g.lower() in wanted}

    if not groups:
        rc.log(log_fh, "no symbols found (is d_us_txt.zip present? check --symbols/--groups)")
        return 1
    symbols = sorted(groups)

    # --- 2. Worklist: skip symbols already in the DB ------------------------
    db_syms = rc.db_symbols(conn, "us") if conn is not None else set()
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
            rc.log(log_fh, f"  {sym:10s} -> data/us.sqlite3")
        if len(todo) > 20:
            rc.log(log_fh, f"  ... and {len(todo) - 20} more")
        rc.log(log_fh, f"DRY RUN done: {len(todo)} symbols at ~{args.rate}/min "
                       f"=> ~{(len(todo) + args.batch - 1) // args.batch} requests "
                       f"=~ {((len(todo) + args.batch - 1) // args.batch * 60) // args.rate // 60} min")
        return 0

    # --- 3. Fetch in rate-limited batches ----------------------------------
    rc.do_login()
    stocks = rc.import_stocks()
    limiter = rc.RateLimiter(args.rate)

    ok = failed = 0
    n_batches = (len(todo) + args.batch - 1) // args.batch
    for i in range(0, len(todo), args.batch):
        chunk = todo[i : i + args.batch]
        batch = rc.fetch_symbol_batch(stocks, chunk, rc.SPAN_FULL, limiter, args.batch)
        b_ok = b_fail = 0
        for sym in chunk:
            bars = batch.get(sym)
            rows = rc.bars_to_rows(sym, bars) if bars else []
            if not rows:
                b_fail += 1
                reason = "no data returned" if sym not in batch else "0 usable rows"
                rc.log(log_fh, f"    FAIL {sym}: {reason}")
                continue
            if conn is not None:
                rc.upsert_rows(conn, rows, "us")
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
