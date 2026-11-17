#!/usr/bin/env bash
# Stop the workstation stand-in submitter (scripts/start_ws_standby.sh): the scheduler loop, the
# scheduler under it, and the guard. The Telegram login broker is left alone.
set -u
cd "$(dirname "$0")/.."
RUN=data/standby
for name in daily guard; do
  f=$RUN/$name.pid
  [ -f "$f" ] || continue
  pid=$(cat "$f")
  if [ "$name" = guard ] && [ "$pid" = "$PPID" ]; then rm -f "$f"; continue; fi   # called by the guard itself
  pkill -TERM -P "$pid" 2>/dev/null                  # the python under the restart loop
  kill -TERM "$pid" 2>/dev/null && echo "stopped $name (pid $pid)"
  rm -f "$f"
done
sleep 2
ps -eo pid,args | grep "[p]pminer daily" | grep "$(pwd)" >/dev/null && echo "warning: a ppminer daily process is still running here" || true
