"""Convert the canonical d_us_txt.zip archive into a single SQLite bars DB.

Produces the same `bars` table (schema + symbol convention) as the Rust
market-data sqlite source so `data_mode = "sqlite"` can read it directly:

    bars(symbol TEXT, market TEXT, date INTEGER, open, high, low, close REAL,
         volume INTEGER, openint INTEGER, PRIMARY KEY(symbol, market, date))

Symbols are stored uppercase zip-style keys (BRK.B -> BRK-B) with market="us".

Usage:
    python convert_zip_to_sqlite.py [--zip d_us_txt.zip]
                                    [--db d_us_txt.sqlite3]

Defaults resolve relative to this project / its sibling folder (see
rh_common.ZIP_PATH), so no absolute paths are hardcoded here.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
import zipfile

import rh_common as rc

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol  TEXT    NOT NULL,
    market  TEXT    NOT NULL,
    date    INTEGER NOT NULL,
    open    REAL    NOT NULL,
    high    REAL    NOT NULL,
    low     REAL    NOT NULL,
    close   REAL    NOT NULL,
    volume  INTEGER NOT NULL,
    openint INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, market, date)
) WITHOUT ROWID
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--zip", default=rc.ZIP_PATH)
    ap.add_argument(
        "--db",
        default=os.path.splitext(rc.ZIP_PATH)[0] + ".sqlite3",
        help="output SQLite db (default: <zip name>.sqlite3 next to the zip)",
    )
    ap.add_argument("--limit", type=int, default=0, help="only first N files (0 = all)")
    ap.add_argument("--overlay", default="",
                    help="sqlite db whose bars are upserted after the zip "
                         "(e.g. rh_data/data/us.sqlite3 - Robinhood-fetched rows)")
    ap.add_argument("--overlay-market", default="us",
                    help="market filter for --overlay source rows (default: us)")
    args = ap.parse_args()

    t0 = time.time()
    zf = zipfile.ZipFile(args.zip)
    members = sorted(
        m for m in zf.namelist() if m.endswith(".us.txt") and not m.endswith("/")
    )
    if args.limit:
        members = members[: args.limit]
    print(f"{len(members)} .us.txt members in {args.zip}")

    conn = sqlite3.connect(args.db)
    conn.execute(_SCHEMA)
    conn.commit()

    total_rows = 0
    n_done = 0
    for m in members:
        try:
            text = zf.read(m).decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            print(f"  skip {m}: {e}")
            continue
        rows = rc.rows_to_db_rows(text.splitlines(), "us")
        if not rows:
            continue
        conn.executemany(
            """INSERT INTO bars (symbol, market, date, open, high, low, close, volume, openint)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (symbol, market, date) DO UPDATE SET
                 open = excluded.open, high = excluded.high, low = excluded.low,
                 close = excluded.close, volume = excluded.volume, openint = excluded.openint""",
            rows,
        )
        total_rows += len(rows)
        n_done += 1
        if n_done % 500 == 0:
            conn.commit()
            el = time.time() - t0
            print(f"  {n_done}/{len(members)} files | {total_rows:,} rows | {el:.0f}s")

    conn.commit()
    conn.close()

    # Optional overlay: upsert bars from another db on top (target market='us').
    if args.overlay and os.path.exists(args.overlay):
        conn = sqlite3.connect(args.db)
        conn.execute("ATTACH DATABASE ? AS ov", (args.overlay,))
        cur = conn.execute(
            """INSERT INTO bars (symbol, market, date, open, high, low, close, volume, openint)
               SELECT symbol, 'us', date, open, high, low, close, volume, openint
               FROM ov.bars WHERE market = ?
               ON CONFLICT (symbol, market, date) DO UPDATE SET
                 open = excluded.open, high = excluded.high, low = excluded.low,
                 close = excluded.close, volume = excluded.volume, openint = excluded.openint""",
            (args.overlay_market,),
        )
        print(f"overlay: {cur.rowcount} rows upserted from {args.overlay} "
              f"(market={args.overlay_market})")
        conn.commit()
        conn.close()

    # quick sanity
    c = sqlite3.connect(args.db)
    cur = c.cursor()
    cur.execute("SELECT COUNT(DISTINCT symbol), COUNT(*), MIN(date), MAX(date) FROM bars")
    print("sanity (symbols, rows, min, max):", cur.fetchone())
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
