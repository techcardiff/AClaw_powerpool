#!/usr/bin/env bash
# Start the Power Pool miner services on the 3080 in tmux session "ppminer".
# Each window restarts its service if it exits. Idempotent: creates the session if
# missing and re-adds any window that is gone, so it is safe to run any time.
#
#   windows: qwen-tunnel  ssh tunnel 127.0.0.1:18060 -> A6000 127.0.0.1:8060 (Qwen3.8)
#            web          dashboard on 127.0.0.1:5070 (view from the Mac: ssh -N -L 5070:127.0.0.1:5070 qa-3080)
#            daily        scheduler: Power Pool auto-submit after midnight New York, mining the rest of
#                         the day (config data/autosubmit.json; the only thing that submits)
#
# To restart a service, kill its window (tmux kill-window -t ppminer:web) and run this
# script again. Don't pkill by pattern: the window's restart loop matches it too.
#
# The Telegram broker can't run here (api.telegram.org is blocked from this network);
# it runs on the Mac and copies fresh session cookies into data/secrets/.
set -u
ROOT=/data/aclaw_powerpool
PY=/data/quant-agent/envs/orchestrator/bin/python
A6000=192.168.112.204
LOG=$ROOT/data/logs
mkdir -p "$LOG"

TUNNEL="while true; do ssh -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -N -L 18060:127.0.0.1:8060 $A6000 >> $LOG/qwen_tunnel.log 2>&1; echo \"\$(date) tunnel exited, restarting\" >> $LOG/qwen_tunnel.log; sleep 10; done"
WEB="cd $ROOT && while true; do PP_BROKER_ELSEWHERE=1 PYTHONUNBUFFERED=1 $PY -m ppminer web --port 5070 >> $LOG/web.log 2>&1; \
  echo \"\$(date) web exited, restarting\" >> $LOG/web.log; sleep 5; done"
DAILY="cd $ROOT && while true; do PYTHONUNBUFFERED=1 $PY -m ppminer daily >> $LOG/daily.log 2>&1; \
  echo \"\$(date) daily exited, restarting\" >> $LOG/daily.log; sleep 30; done"

ensure() {   # ensure <window-name> <command>
  if ! tmux has-session -t ppminer 2>/dev/null; then
    tmux new-session -d -s ppminer -n "$1" "$2"; echo "started session ppminer with $1"
  elif ! tmux list-windows -t ppminer -F '#W' | grep -qx "$1"; then
    tmux new-window -t ppminer -n "$1" "$2"; echo "added window $1"
  else
    echo "$1 already running"
  fi
}
ensure qwen-tunnel "$TUNNEL"
ensure web "$WEB"
ensure daily "$DAILY"
