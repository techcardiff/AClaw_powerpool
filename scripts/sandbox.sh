#!/usr/bin/env bash
# A student's sandbox on the 3080 (Song's: /data/aclaw_song, branch "song", port 5071).
# Its own git checkout and its own copy of the database: change code, templates, lanes,
# scoring as much as you like without touching the live system. It simulates on BRAIN
# (HT52122, sharing the live session cookie) but can never submit (PP_SANDBOX=1).
# The live dashboard shows it at https://alpha.hokin.cloud/song/ (same password).
#
#   bash /data/aclaw_powerpool/scripts/sandbox.sh create [bundle]   clone into /data/aclaw_song, branch song, copy data
#   bash /data/aclaw_song/scripts/sandbox.sh start            start the sandbox web page (tmux session "song")
#   bash /data/aclaw_song/scripts/sandbox.sh start daily      also start the sandbox's own scheduler (mining loop)
#   bash /data/aclaw_song/scripts/sandbox.sh stop             stop everything in tmux session "song"
#   bash /data/aclaw_song/scripts/sandbox.sh refresh-db       replace the sandbox database with a fresh live copy
#
# After changing code, restart: sandbox.sh stop && sandbox.sh start [daily].
# Shared with the live system and NOT to be changed from here: /data/aclaw_powerpool,
# the tmux session "ppminer", data/secrets (session cookies), the Qwen tunnel on :18060.
set -eu
NAME=song
PORT=5071
LIVE=/data/aclaw_powerpool
REPO=https://github.com/techcardiff/AClaw_powerpool.git
SBX=/data/aclaw_$NAME
PY=/data/quant-agent/envs/orchestrator/bin/python
LOG=$SBX/data/logs

copy_db() {   # consistent copy of the live database while the live scheduler is writing to it
  "$PY" - "$LIVE/data/pp.db" "$SBX/data/pp.db" <<'EOF'
import os, sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(src, timeout=120)
d = sqlite3.connect(dst + ".tmp")
s.backup(d)
d.close(); s.close()
for ext in ("-wal", "-shm"):
    if os.path.exists(dst + ext):
        os.remove(dst + ext)
os.replace(dst + ".tmp", dst)
print(f"copied {src} -> {dst}")
EOF
}

case "${1:-}" in
  create)
    [ -e "$SBX" ] && { echo "$SBX already exists"; exit 1; }
    # GitHub is flaky from the cluster; a bundle made elsewhere also works:
    #   git bundle create main.bundle main   (on a laptop), copy it over, then: sandbox.sh create main.bundle
    git clone "${2:-$REPO}" "$SBX"
    git -C "$SBX" remote set-url origin "$REPO"            # pushing needs your own GitHub login
    git -C "$SBX" checkout -b "$NAME"
    mkdir -p "$SBX/data/logs" "$SBX/data/reports"
    cp -r "$LIVE/data/catalog" "$SBX/data/catalog"
    ln -s "$LIVE/data/secrets" "$SBX/data/secrets"          # cookies refreshed by the broker reach both
    "$PY" - "$LIVE/data/autosubmit.json" "$SBX/data/autosubmit.json" <<'EOF'
import json, sys
cfg = json.load(open(sys.argv[1]))
for a in cfg.get("accounts", {}).values():
    a["dry_run"] = True          # PP_SANDBOX forces this anyway
cfg["_comment"] = "Sandbox copy: accounts/regions here drive the sandbox scheduler; it never submits."
json.dump(cfg, open(sys.argv[2], "w"), indent=2)
EOF
    copy_db
    echo "created $SBX on branch $NAME; start it with: bash $SBX/scripts/sandbox.sh start"
    ;;
  start)
    cd "$SBX"
    mkdir -p "$LOG"
    ENV="PP_SANDBOX=1 PP_SANDBOX_NAME=$NAME PYTHONUNBUFFERED=1"
    WEB="cd $SBX && while true; do $ENV $PY -m ppminer web --port $PORT >> $LOG/web.log 2>&1; \
      echo \"\$(date) web exited, restarting\" >> $LOG/web.log; sleep 5; done"
    DAILY="cd $SBX && while true; do $ENV $PY -m ppminer daily >> $LOG/daily.log 2>&1; \
      echo \"\$(date) daily exited, restarting\" >> $LOG/daily.log; sleep 30; done"
    ensure() {
      if ! tmux has-session -t "$NAME" 2>/dev/null; then
        tmux new-session -d -s "$NAME" -n "$1" "$2"; echo "started session $NAME with $1"
      elif ! tmux list-windows -t "$NAME" -F '#W' | grep -qx "$1"; then
        tmux new-window -t "$NAME" -n "$1" "$2"; echo "added window $1"
      else
        echo "$1 already running"
      fi
    }
    ensure web "$WEB"
    [ "${2:-}" = daily ] && ensure daily "$DAILY"
    echo "open https://alpha.hokin.cloud/song/  (logs: $LOG)"
    ;;
  stop)
    tmux kill-session -t "$NAME" 2>/dev/null && echo "stopped session $NAME" || echo "session $NAME not running"
    ;;
  refresh-db)
    tmux has-session -t "$NAME" 2>/dev/null && { echo "stop the sandbox first: bash $0 stop"; exit 1; }
    copy_db
    ;;
  *)
    sed -n '2,19p' "$0"; exit 1
    ;;
esac
