"""Power Pool themes from BRAIN's support article, written to data/themes.json.

  python -m ppminer themes                    # session from data/secrets (the broker's machine)
  python -m ppminer themes --from-host qa-3080  # session read from the 3080 over SSH
  python -m ppminer themes --dry-run          # fetch and parse, write nothing

The article ("Current month Power Pool Themes") is a calendar: one row of dates per
week, then that week's theme as 'NAME: rule'. It is for signed-in users only and the
page sits behind Cloudflare, so a plain HTTP request gets 403/401. Like
scripts/open_brain_session.py, this starts Google Chrome with a temporary profile, puts
the account owner's own BRAIN session into it (the support site signs in from it) and
reads the page text; the profile is deleted afterwards. If Cloudflare asks for a
challenge, it gives up: nothing here solves or works around one.

Runs next to the Telegram broker (scripts/start_ws_broker.sh), which keeps the session
fresh; PP_THEMES_SYNC (e.g. 'qa-3080:/data/aclaw_powerpool/data/') copies the result to
the machine running the scheduler. theme.current() reads data/themes.json over the
repository's themes.json.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request

from . import cookies as ck

ARTICLE_URL = "https://support.worldquantbrain.com/hc/en-us/articles/38927747787031-Current-month-Power-Pool-Themes"
ACCOUNT = "HT52122"                       # the owner's own session
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_THEMES = os.path.join(ROOT, "data", "themes.json")
CHROME_PATHS = {
    "Darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
    "Linux": ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"],
    "Windows": [r"C:\Program Files\Google\Chrome\Application\chrome.exe"],
}
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


class FetchError(RuntimeError):
    pass


# ── parsing ─────────────────────────────────────────────────────────────────
def norm_name(name: str) -> str:
    """The article writes Nov'26, BRAIN's checks Nov`26."""
    return re.sub(r"[`'’‘]", "`", name.strip())


def _date_row(line: str) -> list[str] | None:
    cells = [c.strip() for c in line.split("\t")]
    cells = [c for c in cells if c]
    if not cells or not all(re.fullmatch(r"\d{1,2}(?: [A-Za-z]{3})?", c) for c in cells):
        return None
    return cells


def parse(text: str, year: int) -> list[dict]:
    """[{name, start, end, rule}] from the article text, one entry per theme spanning
    the weeks it is listed in. `year` is the year of the first week's dates."""
    weeks: list[tuple[dt.date, dt.date, list[str]]] = []
    month, last_day, cur = None, 0, None
    for line in text.splitlines():
        if (cells := _date_row(line)) is not None:
            days = []
            for c in cells:
                d, _, mon = c.partition(" ")
                if mon:
                    month = MONTHS[mon.lower()]
                elif month is not None and int(d) < last_day:
                    month = month % 12 + 1
                    year += month == 1
                if month is None:
                    raise FetchError(f"calendar row without a month: {line!r}")
                last_day = int(d)
                days.append(dt.date(year, month, int(d)))
            cur = (days[0], days[-1], [])
            weeks.append(cur)
        elif cur is not None and ":" in line and "=" in line:
            name, _, rule = line.strip().partition(":")
            cur[2].append(f"{norm_name(name)}\t{rule.strip()}")
    spans: dict[str, dict] = {}
    for start, end, themes in weeks:
        for t in themes:
            name, rule = t.split("\t")
            s = spans.setdefault(name, {"name": name, "start": start, "end": end, "rule": rule})
            s["start"], s["end"] = min(s["start"], start), max(s["end"], end)
    return [dict(s, start=s["start"].isoformat(), end=s["end"].isoformat()) for s in spans.values()]


def merge(old: list[dict], new: list[dict]) -> list[dict]:
    """New entries replace old ones of the same name; past themes are kept."""
    names = {norm_name(n["name"]) for n in new}
    return sorted([o for o in old if norm_name(o["name"]) not in names] + new, key=lambda r: r["start"])


# ── Chrome ──────────────────────────────────────────────────────────────────
def _chrome() -> str:
    """PP_CHROME (e.g. Chrome for Testing unpacked without sudo), else an installed Chrome."""
    if (p := os.getenv("PP_CHROME")) and os.path.exists(p):
        return p
    for c in CHROME_PATHS.get(platform.system(), []):
        p = c if os.path.isabs(c) else shutil.which(c)
        if p and os.path.exists(p):
            return p
    raise FetchError("Google Chrome not found")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _json(url: str):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


def _cdp(ws, msg_id: int, method: str, params: dict) -> dict:
    ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
    while True:
        reply = json.loads(ws.recv())
        if reply.get("id") == msg_id:
            if "error" in reply:
                raise FetchError(f"Chrome refused {method}: {reply['error'].get('message')}")
            return reply.get("result", {})


def page_text(token: str, hours_left: float, url: str = ARTICLE_URL, wait: float = 90) -> str:
    """The article's visible text, read from a temporary Chrome profile holding the session."""
    import websocket                               # pip install websocket-client
    profile, port = tempfile.mkdtemp(prefix="brain-themes-"), _free_port()
    chrome = subprocess.Popen([_chrome(), f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
                               "--remote-allow-origins=*", "--no-first-run", "--no-default-browser-check",
                               "--disable-background-mode", "about:blank"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 20
        while True:
            try:
                ws_url = _json(f"http://127.0.0.1:{port}/json/version")["webSocketDebuggerUrl"]
                break
            except Exception:
                if time.time() > deadline:
                    raise FetchError("Chrome did not start its DevTools endpoint")
                time.sleep(0.3)
        ws = websocket.create_connection(ws_url, timeout=30)
        _cdp(ws, 1, "Storage.setCookies", {"cookies": [{
            "name": "t", "value": token, "domain": ".worldquantbrain.com", "path": "/", "secure": True,
            "httpOnly": True, "sameSite": "Lax", "expires": time.time() + hours_left * 3600}]})
        target = _cdp(ws, 2, "Target.createTarget", {"url": url})["targetId"]
        ws.close()
        deadline = time.time() + wait
        while time.time() < deadline:
            time.sleep(3)
            page = next((p for p in _json(f"http://127.0.0.1:{port}/json/list") if p.get("id") == target), None)
            if page is None:
                continue
            pw = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=30)
            res = _cdp(pw, 1, "Runtime.evaluate", {"returnByValue": True, "expression":
                       "JSON.stringify({t: document.title, b: document.body ? document.body.innerText : ''})"})
            pw.close()
            d = json.loads(res["result"]["value"])
            if "Power Pool" in d["t"] and "\t" in d["b"]:
                return d["b"]
        raise FetchError(f"article did not load (last title {d['t']!r}: a Cloudflare check or a sign-in page)")
    finally:
        if chrome.poll() is None:
            chrome.terminate()
            try:
                chrome.wait(timeout=10)
            except subprocess.TimeoutExpired:
                chrome.kill()
        shutil.rmtree(profile, ignore_errors=True)


# ── session ─────────────────────────────────────────────────────────────────
def _token(account: str, host: str | None) -> str:
    if host:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host,
                            f"cat /data/aclaw_powerpool/data/secrets/{account}.cookie"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise FetchError(f"could not read the session from {host}")
        cookie = r.stdout
    else:
        cookie = ck.load_cookie(account)
    return ck.only_session(cookie).partition("=")[2]


def session(account: str = ACCOUNT, host: str | None = None) -> tuple[str, float]:
    """(token, hours left) of the account owner's live session; refuses any other account."""
    token = _token(account, host)
    who = ck.whoami(f"t={token}")
    if not who or who["user_id"] != account:
        raise FetchError(f"no live {account} session; log in again (/login {account} to the Telegram bot)")
    return token, (who["expiry_seconds"] or 0) / 3600


def run(account: str = ACCOUNT, host: str | None = None, dry_run: bool = False, path: str = DATA_THEMES) -> list[dict]:
    token, hours = session(account, host)
    text = page_text(token, hours)
    del token
    m = re.search(r"\b([A-Z][a-z]+ \d{1,2}, (\d{4}))", text)         # 'October 31, 2026 17:00 Updated'
    year = int(m.group(2)) if m else dt.date.today().year
    first = re.search(r"\b\d{1,2} ([A-Za-z]{3})\b", text)
    if first and m and MONTHS[first.group(1).lower()] > dt.datetime.strptime(m.group(1), "%B %d, %Y").month:
        year -= 1                                                  # first week in December, updated in January
    themes = parse(text, year)
    if not themes:
        raise FetchError("no themes found in the article")
    if dry_run:
        return themes
    old = json.load(open(path)) if os.path.exists(path) else []
    rows = merge(old, themes)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rows, f, indent=1)
    os.replace(tmp, path)
    if target := os.getenv("PP_THEMES_SYNC"):
        p = subprocess.run(["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", path, target],
                           capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            raise FetchError(f"themes sync to {target} failed: {p.stderr.strip()[:200]}")
    return themes
