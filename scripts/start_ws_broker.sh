#!/usr/bin/env bash
# Telegram login broker on the workstation (Telegram reachable via the Tailscale exit node).
# Restarts itself if it exits; each new session cookie is copied to the 3080.
set -u
cd "$(dirname "$0")/.."
if pgrep -f "[.]venv/bin/python -m ppminer telegram run" >/dev/null; then echo "broker already running"; exit 0; fi
mkdir -p data/logs
nohup bash -c "while true; do PP_COOKIE_SYNC=qa-3080:/data/aclaw_powerpool/data/secrets/ PYTHONUNBUFFERED=1 .venv/bin/python -m ppminer telegram run >> data/logs/telegram.log 2>&1; echo \"\$(date) broker exited, restarting in 30s\" >> data/logs/telegram.log; sleep 30; done" > /dev/null 2>&1 &
echo "broker started (loop pid $!)"
