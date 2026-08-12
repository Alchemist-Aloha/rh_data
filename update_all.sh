#!/usr/bin/env bash
# Nightly rh_data update:
#   1. update_daily.py            -> data/daily/us/ tree + data/us.sqlite3, and
#      overlays the fresh US tree onto d_us_txt.zip -> d_us_txt_merged.zip
#   2. update_adr_daily.py        -> data/daily/adr/ tree + data/adr.sqlite3
#   3. update_robinhood_daily.py  -> data/daily/robinhood/ + data/robinhood.sqlite3
#   4. convert_zip_to_sqlite.py   -> rebuild d_us_txt.sqlite3 from the merged zip
# Idempotent (UPSERT PK symbol,market,date) - safe to rerun.
#
# Exit codes: 0 ok, 1 hard failure. Symbol-level FAILs (exit 2 from the
# updaters, e.g. delisted symbols) are normal and don't fail the run.
set -u
cd "$(dirname "$0")" || exit 1
HERE="$(pwd)"
ROOT="$(dirname "$HERE")"            # QuantTrading
MERGED="$ROOT/d_us_txt_merged.zip"  # source zip + fresh pair-1 overlay
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

step us        "${PY[@]}" update_daily.py --zip-out "$MERGED"
step adr       "${PY[@]}" update_adr_daily.py
step robinhood "${PY[@]}" update_robinhood_daily.py
if [ -f "$MERGED" ]; then
  step rebuild "${PY[@]}" convert_zip_to_sqlite.py --zip "$MERGED" --db "$ROOT/d_us_txt.sqlite3"
else
  echo "!! $MERGED missing - skipping rebuild"; rc=1
fi

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
