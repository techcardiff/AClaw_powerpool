#!/usr/bin/env bash
# Start the Telegram login broker on the Mac (Telegram is blocked from the cluster).
# Every fresh session cookie is copied to the 3080, where the miner runs.
# caffeinate keeps the Mac awake while the broker runs.
# Next to it, every 6 hours: the Power Pool themes from BRAIN's support article, copied to the 3080.
set -u
cd "$(dirname "$0")/.."
mkdir -p data/logs
if ! pgrep -f "THEMES_LOOP" >/dev/null; then
  nohup bash -c "THEMES_LOOP=1; while true; do PP_THEMES_SYNC=qa-3080:/data/aclaw_powerpool/data/ .venv/bin/python -m ppminer themes --from-host qa-3080 >> data/logs/themes.log 2>&1; sleep 21600; done" > /dev/null 2>&1 &
  echo "themes fetch started (loop pid $!)"
fi
if pgrep -f "ppminer telegram run" >/dev/null; then
  echo "broker already running"; exit 0
fi
PP_COOKIE_SYNC="qa-3080:/data/aclaw_powerpool/data/secrets/" PYTHONUNBUFFERED=1 \
  nohup caffeinate -i .venv/bin/python -m ppminer telegram run >> data/logs/telegram.log 2>&1 &
echo "broker started (pid $!), cookies sync to qa-3080"
