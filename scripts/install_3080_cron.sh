#!/usr/bin/env bash
# Add an @reboot entry (once) so the ppminer tmux session comes back after a 3080 restart.
set -eu
LINE="@reboot /data/aclaw_powerpool/scripts/start_3080.sh >> /data/aclaw_powerpool/data/logs/start.log 2>&1"
( crontab -l 2>/dev/null | grep -vF "/data/aclaw_powerpool/scripts/start_3080.sh" ; echo "$LINE" ) | crontab -
crontab -l | grep -F start_3080.sh
