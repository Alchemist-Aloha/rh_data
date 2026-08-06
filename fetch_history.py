"""Fetch the maximum daily history for every symbol from Robinhood into rh_data/.

Writes files that mirror d_us_txt.zip's structure and row format:

    rh_data/data/daily/us/<group>/<symbol>.us.txt

Rate-limited to <=10 HTTP requests/minute by default (--rate to override).
Symbols that already have a non-empty file are skipped (--refresh to refetch),
so the script is resumable after an interrupted run.

Usage:
    python fetch_history.py                    # full universe from d_us_txt.zip
    python fetch_history.py --limit 20         # first 20 symbols (test run)
    python fetch_history.py --symbols AAPL,MSFT
    python fetch_history.py --groups "nasdaq etfs,nysemkt stocks"
    python fetch_history.py --zip-path <archive.zip>   # custom source archive
    python fetch_history.py --refresh          # overwrite existing files
    python fetch_history.py --dry-run          # plan only, no network
    python fetch_history.py --zip-out d_us_txt_merged.zip
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
    ap.add_argument("--refresh", action="store_true", help="refetch even if the file already exists")
    ap.add_argument("--dry-run", action="store_true", help="print the plan without fetching")
    ap.add_argument("--zip-path", default=rc.ZIP_PATH,
                    help="source archive for symbol discovery (default: project dir, else sibling)")
    ap.add_argument("--zip-out", default="", help="also write a merged zip (overlays the source archive)")
    ap.add_argument("--log", default=os.path.join(rc.HERE, "fetch_history.log"))
    args = ap.parse_args()

    os.makedirs(rc.DATA_ROOT, exist_ok=True)
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

    # --- 2. Worklist: skip files that already exist ------------------------
    todo: list[tuple[str, str, str]] = []
    skipped = 0
    for sym in symbols:
        path = rc.out_path(sym, groups[sym])
        if not args.refresh and os.path.exists(path) and rc.read_rows(path):
            skipped += 1
            continue
        todo.append((sym, groups[sym], path))
    if args.limit:
        todo = todo[: args.limit]
    rc.log(log_fh, f"universe={len(symbols)} already_fetched={skipped} to_fetch={len(todo)}")

    if not todo:
        rc.log(log_fh, "nothing to fetch (use --refresh to overwrite existing files)")
        return 0

    if args.dry_run:
        rc.log(log_fh, "DRY RUN - would fetch:")
        for sym, group, path in todo[:20]:
            rc.log(log_fh, f"  {sym:10s} -> {os.path.relpath(path, rc.HERE)}")
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
        syms = [c[0] for c in chunk]
        batch = rc.fetch_symbol_batch(stocks, syms, rc.SPAN_FULL, limiter, args.batch)
        b_ok = b_fail = 0
        for sym, group, path in chunk:
            bars = batch.get(sym)
            rows = rc.bars_to_rows(sym, bars) if bars else []
            if not rows:
                b_fail += 1
                reason = "no data returned" if sym not in batch else "0 usable rows"
                rc.log(log_fh, f"    FAIL {sym}: {reason}")
                continue
            rc.write_rows(path, rows)
            b_ok += 1
        ok += b_ok
        failed += b_fail
        rc.log(log_fh, f"  batch {i // args.batch + 1}/{n_batches} ({len(chunk)} syms): "
                       f"ok={b_ok} fail={b_fail}")

    rc.log(log_fh, f"=== done: ok={ok} failed={failed} (see failed symbols above) ===")

    # --- 4. Optional merged zip --------------------------------------------
    if args.zip_out:
        n = rc.merge_into_zip(args.zip_path, args.zip_out)
        rc.log(log_fh, f"zip written: {args.zip_out} ({n} rh_data members overlaid)")

    log_fh.close()
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
