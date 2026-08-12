#!/bin/bash
# Stooq US daily zip fetch + sqlite convert (unattended).
# Retries the quota-blocked download; converts via the existing pipeline.
set -u
cd /home/likun/QuantTrading/rh_data

# 1. ensure fresh-profile chromium on 9333 is up
if ! curl -sf http://127.0.0.1:9333/json/version >/dev/null 2>&1; then
  rm -rf /tmp/stooq_fresh_profile && mkdir -p /tmp/stooq_fresh_profile
  nohup /usr/bin/chromium --headless=new --no-sandbox --disable-gpu --remote-allow-origins=* \
    --user-data-dir=/tmp/stooq_fresh_profile --remote-debugging-port=9333 about:blank \
    >/tmp/stooq_chromium.log 2>&1 &
  sleep 4
fi

OUT=/home/likun/Downloads/d_us_txt.zip
rm -f "$OUT" /home/likun/Downloads/error*.txt

# 2. run the CDP fetcher (auto vision for captcha)
for attempt in 1 2 3; do
  echo "=== attempt $attempt ==="
  timeout 400 uv run --quiet python stooq_cdp.py; rc=$?
  echo "fetcher rc=$rc"
  if [ -s "$OUT" ]; then break; fi
  if [ $rc -eq 3 ]; then
    # ap pre-set: download-only path
    timeout 400 uv run --quiet python stooq_cdp_dl.py; rc=$?
    if [ -s "$OUT" ]; then break; fi
  fi
  sleep 30
done

# 3. verify
if [ ! -s "$OUT" ]; then
  echo "FAIL: no zip downloaded"; exit 1
fi
file "$OUT" | grep -q "Zip archive" || { echo "FAIL: not a zip"; exit 1; }

# 4. move into place + convert (existing canonical pipeline)
mv -f "$OUT" /home/likun/QuantTrading/d_us_txt.zip
echo "zip: $(ls -la /home/likun/QuantTrading/d_us_txt.zip | awk '{print $5}') bytes"
uv run --quiet python convert_zip_to_sqlite.py --zip /home/likun/QuantTrading/d_us_txt.zip \
  --db /home/likun/QuantTrading/d_us_txt.sqlite3 2>&1 | tail -5
echo "DONE"
