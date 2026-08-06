"""Daily incremental update: fetch the newest daily bars and append them.

For symbols already in rh_data: fetches span='month' (covers up to ~1 missed
month of sessions) and appends only bars newer than the file's last date.
For symbols in d_us_txt.zip that were never fetched: fetches full '5year'
history. Idempotent - safe to run multiple times, nothing is double-counted.

Rate-limited to <=10 HTTP requests/minute by default (--rate to override).

Usage:
    python update_daily.py                    # update everything
    python update_daily.py --rate 20          # faster (Robinhood tolerates more)
    python update_daily.py --dry-run          # plan only, no network
    python update_daily.py --zip-path <archive.zip>   # custom source archive
    python update_daily.py --zip-out d_us_txt_merged.zip
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
    ap.add_argument("--zip-path", default=rc.ZIP_PATH,
                    help="source archive for symbol discovery (default: project dir, else sibling)")
    ap.add_argument("--zip-out", default="", help="also write a merged zip (overlays the source archive)")
    ap.add_argument("--log", default=os.path.join(rc.HERE, "update_daily.log"))
    args = ap.parse_args()

    os.makedirs(rc.DATA_ROOT, exist_ok=True)
    log_fh = open(args.log, "a", encoding="utf-8")
    rc.log(log_fh, f"=== update_daily start (rate={args.rate}/min, batch={args.batch}) ===")

    # --- 1. Universe: local files + any zip symbols never fetched ----------
    if not os.path.exists(args.zip_path):
        rc.log(log_fh, f"zip path not found: {args.zip_path}")
        return 1
    groups = rc.discover_symbols_from_tree()
    zip_groups = rc.discover_symbols_from_zip(args.zip_path)
    for sym, g in zip_groups.items():
        groups.setdefault(sym, g)
    if not groups:
        rc.log(log_fh, "no symbols found (run fetch_history.py first)")
        return 1
    symbols = sorted(groups)

    # --- 2. Split into never-fetched (full history) vs incremental ---------
    missing: list[tuple[str, str, str]] = []
    existing: list[tuple[str, str, str, int]] = []
    for sym in symbols:
        path = rc.out_path(sym, groups[sym])
        last = rc.last_date_in_file(path)
        if last is None:
            missing.append((sym, groups[sym], path))
        else:
            existing.append((sym, groups[sym], path, last))

    rc.log(log_fh, f"universe={len(symbols)} incremental={len(existing)} missing={len(missing)}")

    if not existing and not missing:
        rc.log(log_fh, "nothing to update")
        return 0

    if args.dry_run:
        rc.log(log_fh, "DRY RUN - would update:")
        for sym, group, path, last in existing[:20]:
            rc.log(log_fh, f"  {sym:10s} last={last} -> {os.path.relpath(path, rc.HERE)}")
        if len(existing) > 20:
            rc.log(log_fh, f"  ... and {len(existing) - 20} more incremental symbols")
        for sym, group, path in missing[:10]:
            rc.log(log_fh, f"  {sym:10s} (NEW, full history) -> {os.path.relpath(path, rc.HERE)}")
        if len(missing) > 10:
            rc.log(log_fh, f"  ... and {len(missing) - 10} more new symbols")
        total = len(existing) + len(missing)
        n_req = (total + args.batch - 1) // args.batch
        rc.log(log_fh, f"DRY RUN done: {total} symbols at ~{args.rate}/min "
                       f"=> ~{n_req} requests =~ {n_req * 60 // args.rate // 60} min")
        return 0

    # --- 3. Fetch + append --------------------------------------------------
    rc.do_login()
    stocks = rc.import_stocks()
    limiter = rc.RateLimiter(args.rate)

    total_added = 0
    failed = 0

    # Pass 1: never-fetched symbols -> full '5year' history
    if missing:
        rc.log(log_fh, f"pass 1: full history for {len(missing)} new symbols")
        for i in range(0, len(missing), args.batch):
            chunk = missing[i : i + args.batch]
            batch = rc.fetch_symbol_batch(stocks, [c[0] for c in chunk], rc.SPAN_FULL, limiter, args.batch)
            for sym, group, path in chunk:
                rows = rc.bars_to_rows(sym, batch.get(sym) or [])
                if not rows:
                    failed += 1
                    rc.log(log_fh, f"    FAIL {sym}: no data")
                    continue
                rc.write_rows(path, rows)
                total_added += len(rows)
            rc.log(log_fh, f"  batch {i // args.batch + 1}: {len(chunk)} new symbols done")

    # Pass 2: existing symbols -> span='month', append bars newer than last date
    if existing:
        rc.log(log_fh, f"pass 2: incremental update for {len(existing)} symbols")
        for i in range(0, len(existing), args.batch):
            chunk = existing[i : i + args.batch]
            batch = rc.fetch_symbol_batch(stocks, [c[0] for c in chunk], rc.SPAN_UPDATE, limiter, args.batch)
            b_added = b_fail = 0
            for sym, group, path, last in chunk:
                bars = batch.get(sym)
                rows = rc.bars_to_rows(sym, bars or [])
                if not rows:
                    if sym not in batch:
                        b_fail += 1
                        rc.log(log_fh, f"    FAIL {sym}: no data returned")
                    continue
                added = rc.append_new_rows(path, rows)
                b_added += added
                if added:
                    rc.log(log_fh, f"    +{added:3d} bars {sym} (was last {last})")
            total_added += b_added
            failed += b_fail
            rc.log(log_fh, f"  batch {i // args.batch + 1}: {len(chunk)} symbols checked, "
                           f"{b_added} new bars, {b_fail} failed")

    rc.log(log_fh, f"=== done: bars_added={total_added} failed={failed} ===")

    # --- 4. Optional merged zip --------------------------------------------
    if args.zip_out:
        n = rc.merge_into_zip(args.zip_path, args.zip_out)
        rc.log(log_fh, f"zip written: {args.zip_out} ({n} rh_data members overlaid)")

    log_fh.close()
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
