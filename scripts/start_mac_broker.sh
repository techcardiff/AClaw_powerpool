#!/usr/bin/env bash
# Start the Telegram login broker on the Mac (Telegram is blocked from the cluster).
# Every fresh session cookie is copied to the 3080, where the miner runs.
# caffeinate keeps the Mac awake while the broker runs.
set -u
cd "$(dirname "$0")/.."
if pgrep -f "ppminer telegram run" >/dev/null; then
  echo "broker already running"; exit 0
fi
mkdir -p data/logs
PP_COOKIE_SYNC="qa-3080:/data/aclaw_powerpool/data/secrets/" PYTHONUNBUFFERED=1 \
  nohup caffeinate -i .venv/bin/python -m ppminer telegram run >> data/logs/telegram.log 2>&1 &
echo "broker started (pid $!), cookies sync to qa-3080"
