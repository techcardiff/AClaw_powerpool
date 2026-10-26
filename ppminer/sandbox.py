"""Sandbox copies of the miner (e.g. Song's, /data/aclaw_song; scripts/sandbox.sh) run with
PP_SANDBOX=1: their own checkout and database, simulating on BRAIN like the live system,
but they can never submit. Only the live scheduler in /data/aclaw_powerpool submits."""
import os

ON = os.getenv("PP_SANDBOX", "") not in ("", "0")
NAME = os.getenv("PP_SANDBOX_NAME", "sandbox")
