#!/usr/bin/env python3
"""Open your own BRAIN session (HT52122) in a throwaway Chrome profile on another computer.

    python3 open_brain_session.py              # fetch, verify, open Chrome, clean up on exit
    python3 open_brain_session.py --dry-run    # fetch + verify only, no browser

How it works
  1. Reads the live session cookie from the 3080 over SSH (the Telegram broker keeps
     it fresh there). The token stays in memory and is never printed or written here.
  2. Asks BRAIN who the session belongs to; refuses unless it is HT52122 and live.
  3. Starts Chrome with a new temporary profile (separate from your normal Chrome),
     puts the session into it via Chrome's DevTools protocol, opens the platform.
  4. Waits until that Chrome is closed, then deletes the temporary profile.

Only for the account owner's own session on the owner's own computers. Other
accounts are refused on purpose: someone else's session must go through their
own face-scan login (e.g. the Telegram bot).

Needs: Python 3.9+, Google Chrome, SSH access to the 3080 (alias qa-3080),
and `pip install websocket-client`.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ALLOWED_ACCOUNT = "HT52122"            # owner's own account only
REMOTE_DIR = "/data/aclaw_powerpool/data/secrets"
AUTH_URL = "https://api.worldquantbrain.com/authentication"
PLATFORM_URL = "https://platform.worldquantbrain.com/"

CHROME_PATHS = {
    "Darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
    "Linux": ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"],
    "Windows": [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")],
}


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def fetch_token(host: str, account: str) -> str:
    """Read <account>.cookie on the 3080 and return only the `t` session value."""
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host,
                        f"cat {REMOTE_DIR}/{account}.cookie"], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        die(f"could not read the session from {host} (SSH access to the 3080 is required)")
    for part in r.stdout.split(";"):
        k, _, v = part.strip().partition("=")
        if k == "t" and v:
            return v.strip()
    die("no session token in the stored cookie")


def verify(token: str, account: str) -> float:
    """Return hours left; refuse anything that is not the allowed account's live session."""
    req = urllib.request.Request(AUTH_URL, headers={"Cookie": f"t={token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status, body = resp.status, resp.read()
    except Exception as e:
        die(f"BRAIN did not accept the session ({e})")
    if status != 200 or not body.strip():
        die("the session is not live; log in again (e.g. /login HT52122 to the Telegram bot), then retry")
    d = json.loads(body)
    user = (d.get("user") or {}).get("id")
    if user != account:
        die(f"the session belongs to {user!r}, not {account}; refusing")
    return ((d.get("token") or {}).get("expiry") or 0) / 3600


def find_chrome() -> str:
    for c in CHROME_PATHS.get(platform.system(), []):
        p = c if os.path.isabs(c) else shutil.which(c)
        if p and os.path.exists(p):
            return p
    die("Google Chrome not found")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def devtools_ws(port: int, timeout: float = 20) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
                return json.loads(r.read())["webSocketDebuggerUrl"]
        except Exception:
            time.sleep(0.3)
    die("Chrome did not start its DevTools endpoint")


def cdp(ws, msg_id: int, method: str, params: dict) -> dict:
    ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
    while True:
        reply = json.loads(ws.recv())
        if reply.get("id") == msg_id:
            if "error" in reply:
                die(f"Chrome refused {method}: {reply['error'].get('message')}")
            return reply.get("result", {})


def wait_for_windows_closed(chrome: subprocess.Popen, port: int) -> None:
    """Return once Chrome exits or has no open page left. On macOS closing the last
    window does not quit Chrome, so the DevTools page list is the reliable signal."""
    while chrome.poll() is None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as r:
                pages = [t for t in json.loads(r.read()) if t.get("type") == "page"]
        except Exception:
            pages = None                     # DevTools gone: Chrome is shutting down
        if not pages:
            return
        time.sleep(2)


def profile_pids(profile: str) -> list[int]:
    """PIDs of any Chrome process (helpers included) using this profile directory."""
    try:
        if platform.system() == "Windows":
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  f"Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*{profile}*' }} "
                                  "| ForEach-Object { $_.ProcessId }"], capture_output=True, text=True, timeout=30).stdout
        else:
            out = subprocess.run(["pgrep", "-f", profile], capture_output=True, text=True, timeout=30).stdout
        return [int(p) for p in out.split() if p.strip().isdigit() and int(p) != os.getpid()]
    except Exception:
        return []


def stop_profile_processes(chrome: subprocess.Popen, profile: str) -> None:
    if chrome.poll() is None:
        chrome.terminate()
        try:
            chrome.wait(timeout=10)
        except subprocess.TimeoutExpired:
            chrome.kill()
    for _ in range(20):                      # helpers, or a background relaunch, can outlive the main process
        pids = profile_pids(profile)
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, 15)
            except OSError:
                pass
        time.sleep(0.5)


def remove_dir(path: str) -> bool:
    for _ in range(10):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return True
        time.sleep(1)
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="qa-3080", help="SSH host holding the session (default qa-3080)")
    ap.add_argument("--dry-run", action="store_true", help="fetch and verify only; do not open Chrome")
    ap.add_argument("--keep-profile", action="store_true", help="do not delete the temporary profile on exit")
    args = ap.parse_args()

    token = fetch_token(args.host, ALLOWED_ACCOUNT)
    hours = verify(token, ALLOWED_ACCOUNT)
    print(f"session for {ALLOWED_ACCOUNT} is live ({hours:.1f}h left)")
    if args.dry_run:
        print(f"dry run: Chrome found at {find_chrome()}; not opening it")
        return

    try:
        import websocket                              # pip install websocket-client
    except ImportError:
        die("missing package: pip install websocket-client")

    profile = tempfile.mkdtemp(prefix="brain-session-")
    port = free_port()
    # --disable-background-mode: otherwise macOS can relaunch this Chrome in the background
    # after its window closes, keeping the profile (and the session) alive.
    chrome = subprocess.Popen([find_chrome(), f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
                               "--remote-allow-origins=*", "--no-first-run", "--no-default-browser-check",
                               "--disable-background-mode", "about:blank"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws = websocket.create_connection(devtools_ws(port), timeout=20)
        cdp(ws, 1, "Storage.setCookies", {"cookies": [{
            "name": "t", "value": token, "domain": ".worldquantbrain.com", "path": "/",
            "secure": True, "httpOnly": True, "sameSite": "Lax",
            "expires": time.time() + hours * 3600}]})
        cdp(ws, 2, "Target.createTarget", {"url": PLATFORM_URL})
        ws.close()
        del token
        print("opened BRAIN in a temporary Chrome profile; close that Chrome window when done")
        wait_for_windows_closed(chrome, port)
    except KeyboardInterrupt:
        pass
    finally:
        stop_profile_processes(chrome, profile)
        if args.keep_profile:
            print(f"temporary profile kept at {profile}")
        elif remove_dir(profile):
            print("temporary profile deleted")
        else:
            print(f"WARNING: could not delete the temporary profile; remove it by hand: {profile}")


if __name__ == "__main__":
    main()
