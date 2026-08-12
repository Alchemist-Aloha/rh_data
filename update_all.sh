#!/usr/bin/env bash
# Nightly rh_data update (SQLite only - no txt files):
#   1. update_daily.py            -> data/us.sqlite3 (full history for new
#      zip symbols, incremental span='month' for stored symbols)
#   2. update_adr_daily.py        -> data/adr.sqlite3
#   3. update_robinhood_daily.py  -> data/robinhood.sqlite3
#   4. convert_zip_to_sqlite.py   -> rebuild d_us_txt.sqlite3 from the stooq
#      zip, then overlay the fresh data/us.sqlite3 rows on top
# Idempotent (UPSERT PK symbol,market,date) - safe to rerun.
#
# Exit codes: 0 ok, 1 hard failure. Symbol-level FAILs (exit 2 from the
# updaters, e.g. delisted symbols) are normal and don't fail the run.
set -u
cd "$(dirname "$0")" || exit 1
HERE="$(pwd)"
ROOT="$(dirname "$HERE")"            # QuantTrading
PY=(uv run --quiet python)

echo "=== rh_data update start $(date '+%F %T %Z') ==="

rc=0
step() {  # <name> <cmd...>
  local name=$1; shift
  local tmp ec
  tmp=$(mktemp)
  echo "--- $name ---"
  if "$@" >"$tmp" 2>&1; then ec=0; else ec=$?; fi
  tail -3 "$tmp"                       # summary lines (detail lives in each script's .log)
  if [ "$ec" -eq 1 ]; then rc=1; echo "!! $name FAILED (exit $ec)"; fi
  rm -f "$tmp"
}

step us        "${PY[@]}" update_daily.py
step adr       "${PY[@]}" update_adr_daily.py
step robinhood "${PY[@]}" update_robinhood_daily.py
step rebuild   "${PY[@]}" convert_zip_to_sqlite.py --zip "$ROOT/d_us_txt.zip" \
                 --db "$ROOT/d_us_txt.sqlite3" --overlay "$HERE/data/us.sqlite3"
step overlay-rh "${PY[@]}" convert_zip_to_sqlite.py --zip "$ROOT/d_us_txt.zip" \
                 --db "$ROOT/d_us_txt.sqlite3" --limit 1 \
                 --overlay "$HERE/data/robinhood.sqlite3" --overlay-market robinhood

# Stooq Current Data snapshot (today's rows) -> d_us_txt.sqlite3. Soft-fail:
# captcha hiccups on this one must not hard-fail the nightly run.
tmp=$(mktemp)
echo "--- stooq-current ---"
if timeout 400 "${PY[@]}" fetch_stooq_current.py >"$tmp" 2>&1; then
  tail -2 "$tmp"
else
  echo "stooq-current skipped (captcha or download issue); rc=$?"
  tail -2 "$tmp"
fi
rm -f "$tmp"

echo "--- final state ---"
ROOT="$ROOT" "${PY[@]}" - <<'EOF'
import os, sqlite3
for rel, m in [("rh_data/data/us.sqlite3", "us"), ("rh_data/data/adr.sqlite3", "adr"),
               ("rh_data/data/robinhood.sqlite3", "robinhood"), ("d_us_txt.sqlite3", "us (zip)")]:
    p = os.path.join(os.environ["ROOT"], rel)
    try:
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        sym, rows, dmin, dmax = c.execute(
            "SELECT COUNT(DISTINCT symbol), COUNT(*), MIN(date), MAX(date) FROM bars").fetchone()
        c.close()
        print(f"  {rel:30s} {m:9s} syms={sym:6d} rows={rows:9d} {dmin}..{dmax}")
    except Exception as e:
        print(f"  {rel:30s} ERROR {e}")
EOF

echo "=== rh_data update done rc=$rc ==="
exit "$rc"
