#!/usr/bin/env bash
# TEMPORARY stand-in submitter on the lab workstation, for while the cluster (3080, A6000) is unreachable.
#   scripts/start_ws_standby.sh          import unsubmitted BRAIN alphas, start the scheduler and the guard
#   scripts/stop_ws_standby.sh           stop both (switch back to the 3080)
# The 3080 stays the real home: its pp.db is untouched, and what the stand-in submits is on BRAIN,
# where the 3080 reads it back (sync_submitted) before its own next submission.
# The guard stops the stand-in by itself as soon as the 3080's SSH port answers again (the 3080
# starts ppminer at boot, @reboot cron), so two submitters never run side by side for more than
# about 5 minutes. It refuses to start while the 3080 is reachable. A port test, not an ssh
# command: the workstation's key on the 3080 is restricted to copying cookies.
# No Qwen here: the scheduler mines with the shape-idea generator (gen.py) instead.
set -u
cd "$(dirname "$0")/.."
ROOT=$(pwd)
PY=$ROOT/.venv/bin/python
LOG=$ROOT/data/logs
RUN=$ROOT/data/standby
mkdir -p "$LOG" "$RUN"

HOST3080=thkclusters.duckdns.org; PORT3080=3080
up3080() { timeout 10 bash -c "</dev/tcp/$HOST3080/$PORT3080" 2>/dev/null; }

alive() { [ -f "$RUN/$1.pid" ] && kill -0 "$(cat "$RUN/$1.pid")" 2>/dev/null; }

if up3080; then
  echo "the 3080 is reachable: it is the submitter, not starting the stand-in"
  exit 1
fi
if [ ! -f data/autosubmit.json ]; then
  echo "data/autosubmit.json is missing (copy autosubmit.example.json and set it up first)"; exit 1
fi

echo "$(date) importing unsubmitted BRAIN alphas" >> "$LOG/standby.log"
$PY -m ppminer import-brain >> "$LOG/standby.log" 2>&1

if alive daily; then
  echo "scheduler already running (pid $(cat "$RUN/daily.pid"))"
else
  nohup bash -c "cd $ROOT && while true; do PYTHONUNBUFFERED=1 $PY -m ppminer daily >> $LOG/daily.log 2>&1; \
    echo \"\$(date) daily exited, restarting\" >> $LOG/daily.log; sleep 30; done" > /dev/null 2>&1 &
  echo $! > "$RUN/daily.pid"
  echo "scheduler started (loop pid $!), log $LOG/daily.log"
fi

if alive guard; then
  echo "guard already running (pid $(cat "$RUN/guard.pid"))"
else
  nohup bash -c "while true; do sleep 300; \
    if timeout 10 bash -c '</dev/tcp/$HOST3080/$PORT3080' 2>/dev/null; then \
      echo \"\$(date) 3080 is back: stopping the stand-in\" >> $LOG/standby.log; $ROOT/scripts/stop_ws_standby.sh >> $LOG/standby.log 2>&1; exit 0; fi; \
    done" > /dev/null 2>&1 &
  echo $! > "$RUN/guard.pid"
  echo "guard started (pid $!): stops the stand-in when the 3080 answers on $HOST3080:$PORT3080"
fi
