#!/usr/bin/env bash
# Telegram login broker on the workstation (Telegram reachable via the Tailscale exit node).
# Restarts itself if it exits; each new session cookie is copied to the 3080.
# Next to it, every 6 hours (30 minutes after a failed fetch, e.g. no live session): the Power Pool themes from BRAIN's support article
# (python -m ppminer themes; needs Google Chrome and a desktop session), copied to the 3080.
set -u
cd "$(dirname "$0")/.."
mkdir -p data/logs
if ! pgrep -f "THEMES_LOOP" >/dev/null; then
  # Chrome for Testing in .chrome, shown on the logged-in desktop (:1). Ubuntu's AppArmor blocks
  # Chrome's namespace sandbox outside /opt, so it uses its setuid helper, made once with sudo:
  #   sudo chown root:root .chrome/chrome-linux64/chrome_sandbox && sudo chmod 4755 .chrome/chrome-linux64/chrome_sandbox
  nohup bash -c "THEMES_LOOP=1; while true; do DISPLAY=\${DISPLAY:-:1} XAUTHORITY=\${XAUTHORITY:-/run/user/\$(id -u)/gdm/Xauthority} CHROME_DEVEL_SANDBOX=\$PWD/.chrome/chrome-linux64/chrome_sandbox PP_CHROME=\$PWD/.chrome/chrome-linux64/chrome PP_THEMES_SYNC=qa-3080:/data/aclaw_powerpool/data/ .venv/bin/python -m ppminer themes >> data/logs/themes.log 2>&1 && sleep 21600 || sleep 1800; done" > /dev/null 2>&1 &
  echo "themes fetch started (loop pid $!)"
fi
# Qwen3.8 (A6000) through the 3080's own Qwen tunnel, for the miner's ideas and the bot's /advise.
# Key ~/.ssh/id_qwen_tunnel is allowed on the 3080 only to forward to 127.0.0.1:18060 (authorized_keys:
# restrict,port-forwarding,permitopen="127.0.0.1:18060",command="/bin/false"; added 23 Nov with the owner's OK).
if [ -f "$HOME/.ssh/id_qwen_tunnel" ] && ! pgrep -f "QWEN_TUNNEL_LOOP" >/dev/null; then
  nohup bash -c "QWEN_TUNNEL_LOOP=1; while true; do ssh -F /dev/null -i \$HOME/.ssh/id_qwen_tunnel -o IdentitiesOnly=yes -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o UserKnownHostsFile=\$HOME/.ssh/known_hosts -p 3080 -N -L 18060:127.0.0.1:18060 alpha@thkclusters.duckdns.org >> data/logs/qwen_tunnel.log 2>&1; sleep 20; done" > /dev/null 2>&1 &
  echo "qwen tunnel started (loop pid $!)"
fi
if pgrep -f "[.]venv/bin/python -m ppminer telegram run" >/dev/null; then echo "broker already running"; exit 0; fi
nohup bash -c "while true; do PP_COOKIE_SYNC=qa-3080:/data/aclaw_powerpool/data/secrets/ PYTHONUNBUFFERED=1 .venv/bin/python -m ppminer telegram run >> data/logs/telegram.log 2>&1; echo \"\$(date) broker exited, restarting in 30s\" >> data/logs/telegram.log; sleep 30; done" > /dev/null 2>&1 &
echo "broker started (loop pid $!)"
