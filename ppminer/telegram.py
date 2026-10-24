"""Telegram login broker: sends BRAIN's face-scan link to each account owner.

Flow per account (same protocol as WorldQuant's own course library):
  1. POST /authentication with the account's email/password (basic auth).
  2. BRAIN answers 401 + `WWW-Authenticate: persona` + a Location link when a
     face scan is needed. The bot sends that link to the OWNER's Telegram chat
     as a "Verify face" button. The face scan is always done by the owner.
  3. The broker re-POSTs the link until BRAIN answers 201, then saves the `t`
     session cookie to data/secrets/<account>.cookie (what resim/explore use).

Config: data/secrets/telegram.json (mode 600), see telegram.example.json.
Run:    python -m ppminer telegram run      (bot + automatic renewals)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import stat
import threading
import time
from urllib.parse import urljoin

import requests

from . import cookies as ck
from .brain import BASE

CONFIG = os.path.join(ck.SECRETS, "telegram.json")
TG_API = "https://api.telegram.org"


class ConfigError(RuntimeError):
    pass


def load_config(path: str = CONFIG) -> dict:
    if not os.path.exists(path):
        raise ConfigError(f"missing {path}; copy telegram.example.json there and fill it in")
    if os.stat(path).st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(f"{path} must be readable only by you: chmod 600 {path}")
    with open(path) as f:
        cfg = json.load(f)
    if not cfg.get("bot_token"):
        raise ConfigError("bot_token missing in telegram.json")
    # "enabled": false parks an account (e.g. a placeholder not filled in yet)
    cfg["accounts"] = {n: a for n, a in cfg.get("accounts", {}).items() if a.get("enabled", True)}
    example_ids = {111111111, 222222222}
    if example_ids & set(cfg.get("admin_chat_ids", [])):
        raise ConfigError("admin_chat_ids still has the example id; put your chat id from @userinfobot")
    for name, a in cfg.get("accounts", {}).items():
        for k in ("chat_id", "email", "password"):
            if not a.get(k):
                raise ConfigError(f"account {name}: '{k}' missing")
        left = [k for k, bad in (("chat_id", a["chat_id"] in example_ids),
                                 ("email", str(a["email"]).endswith("@example.com")),
                                 ("password", a["password"] == "set-by-the-account-owner")) if bad]
        if left:
            raise ConfigError(f"account {name}: still the example {', '.join(left)} "
                              "(fill it in, or delete the account until its owner is ready)")
    return with_defaults(cfg)


DEFAULTS = {
    "renew_before_minutes": 30,
    "quiet_hours": [0, 7],         # local hours [start, end) with no automatic messages
    "scan_wait_minutes": 10,
    "retry_after_minutes": 60,     # resend an unanswered link at most hourly
    "admin_chat_ids": [],          # get copies of everyone's logout / login-ok notices
    "auto_prompt": True,           # false = only notify, owner sends /login themselves
}


def with_defaults(cfg: dict) -> dict:
    return {**DEFAULTS, **cfg}


class Telegram:
    def __init__(self, token: str, http=None):
        self.base = f"{TG_API}/bot{token}"
        self.http = http or requests.Session()

    def call(self, method: str, **params) -> dict:
        r = self.http.post(f"{self.base}/{method}", json=params, timeout=params.get("timeout", 0) + 30)
        d = r.json()
        if not d.get("ok"):
            raise RuntimeError(f"telegram {method}: {d.get('description')}")
        return d["result"]

    def send(self, chat_id, text: str, button: tuple[str, str] | None = None) -> dict:
        params = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if button:
            params["reply_markup"] = {"inline_keyboard": [[{"text": button[0], "url": button[1]}]]}
        return self.call("sendMessage", **params)

    def updates(self, offset: int | None, timeout: int = 30) -> list[dict]:
        return self.call("getUpdates", offset=offset, timeout=timeout, allowed_updates=["message"])


def _hours(sec) -> str:
    return f"{sec / 3600:.1f}h" if sec else "?"


class Broker:
    def __init__(self, cfg: dict, tg: Telegram | None = None, http_factory=requests.Session,
                 now=time.time, sleep=time.sleep):
        self.cfg = with_defaults(cfg)
        self.tg = tg or Telegram(cfg["bot_token"])
        self.http_factory, self.now, self.sleep = http_factory, now, sleep
        self.lock = threading.Lock()
        self.running: set[str] = set()             # accounts with a login in progress
        self.last_prompt: dict[str, float] = {}
        self.logged_out: set[str] = set()          # accounts we already sent a logout notice for

    def notify_admins(self, account: str, text: str) -> None:
        """Copy status news (never scan links) to admins who don't own this account."""
        owner = self.cfg["accounts"][account]["chat_id"]
        for chat in self.cfg["admin_chat_ids"]:
            if chat != owner:
                self.tg.send(chat, text)

    # ── one login ───────────────────────────────────────────────────────────
    def login(self, account: str, reason: str = "") -> bool:
        with self.lock:
            if account in self.running:
                return False
            self.running.add(account)
        try:
            return self._login(account, reason)
        finally:
            with self.lock:
                self.running.discard(account)

    def _login(self, account: str, reason: str) -> bool:
        a = self.cfg["accounts"][account]
        chat = a["chat_id"]
        s = self.http_factory()
        s.auth = (a["email"], a["password"])
        r = s.post(f"{BASE}/authentication", timeout=60)
        if r.status_code == 201:
            return self._finish(account, s, chat, "logged in (no face scan needed)")
        if not (r.status_code == 401 and r.headers.get("WWW-Authenticate") == "persona" and r.headers.get("Location")):
            self.tg.send(chat, f"⚠️ {account}: BRAIN login failed (HTTP {r.status_code}). Check email/password in telegram.json.")
            return False
        link = urljoin(r.url, r.headers["Location"])
        wait = self.cfg["scan_wait_minutes"] * 60
        self.last_prompt[account] = self.now()
        self.tg.send(chat, f"🔐 {account}: BRAIN needs your face scan{(' (' + reason + ')') if reason else ''}.\n"
                           f"Tap the button within {self.cfg['scan_wait_minutes']} min. "
                           "Only you should do this scan.", button=("Verify face", link))
        start, reminded = self.now(), False
        while self.now() - start < wait:
            self.sleep(10)
            if s.post(link, timeout=60).status_code == 201:
                return self._finish(account, s, chat, "face scan accepted")
            if not reminded and self.now() - start > wait / 2:
                self.tg.send(chat, f"⏳ {account}: still waiting for your face scan.", button=("Verify face", link))
                reminded = True
        self.tg.send(chat, f"⌛ {account}: link expired. Send /login {account} to get a new one.")
        return False

    def _finish(self, account: str, s, chat, how: str) -> bool:
        t = s.cookies.get("t")
        who = ck.whoami(f"t={t}") if t else None
        if not who:
            self.tg.send(chat, f"⚠️ {account}: {how}, but no usable session came back. Try /login {account}.")
            return False
        ck.save_cookie(account, f"t={t}")
        self.logged_out.discard(account)
        done = f"✅ {account}: {how}. Session saved, {_hours(who.get('expiry_seconds'))} left."
        self.tg.send(chat, done)
        self.notify_admins(account, done)
        return True

    # ── renewals ────────────────────────────────────────────────────────────
    def quiet(self) -> bool:
        a, b = self.cfg["quiet_hours"]
        h = dt.datetime.fromtimestamp(self.now()).hour
        return a <= h < b if a <= b else (h >= a or h < b)

    def needs_login(self, account: str) -> str | None:
        """Reason string if the account's saved session is missing or about to expire."""
        if account not in ck.saved_accounts():
            return "no saved session"
        who = ck.whoami(ck.load_cookie(account))
        if not who:
            return "session expired"
        left = who.get("expiry_seconds")
        if left is not None and left < self.cfg["renew_before_minutes"] * 60:
            return f"session ends in {left // 60} min"
        return None

    def tick(self) -> list[str]:
        """One scheduler pass: tell owners (and admins) when they are logged out, and start
        logins for accounts that need one. Returns started accounts. Nothing is sent in
        quiet hours; notices held back then go out at the first tick after."""
        if self.quiet():
            return []
        started = []
        for account in self.cfg["accounts"]:
            if account in self.running:
                continue
            reason = self.needs_login(account)
            if reason is None:
                self.logged_out.discard(account)
                continue
            expired = not reason.startswith("session ends")
            if expired and account not in self.logged_out:
                self.logged_out.add(account)
                next_step = ("A face-scan link follows." if self.cfg["auto_prompt"]
                             else f"Send /login {account} when you're ready.")
                self.tg.send(self.cfg["accounts"][account]["chat_id"],
                             f"🔴 {account}: logged out of BRAIN ({reason}). {next_step}")
                self.notify_admins(account, f"🔴 {account}: logged out of BRAIN ({reason}).")
            if not self.cfg["auto_prompt"]:
                continue
            if self.now() - self.last_prompt.get(account, 0) < self.cfg["retry_after_minutes"] * 60:
                continue
            threading.Thread(target=self.login, args=(account, reason), daemon=True).start()
            started.append(account)
        return started

    # ── bot commands ────────────────────────────────────────────────────────
    def allowed(self, chat_id, account: str | None = None) -> bool:
        if chat_id in self.cfg["admin_chat_ids"]:
            return True
        owned = [n for n, a in self.cfg["accounts"].items() if a["chat_id"] == chat_id]
        return bool(owned) and (account is None or account in owned)

    def handle(self, msg: dict) -> None:
        chat = msg["chat"]["id"]
        text = (msg.get("text") or "").strip()
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        if cmd in ("/start", "/id"):
            self.tg.send(chat, f"Your chat id is {chat}. Put it in telegram.json for your account.")
            return
        if not self.allowed(chat):
            return                                   # ignore strangers
        mine = [n for n, a in self.cfg["accounts"].items()
                if a["chat_id"] == chat or chat in self.cfg["admin_chat_ids"]]
        if cmd == "/status":
            lines = []
            for n in mine:
                who = ck.whoami(ck.load_cookie(n)) if n in ck.saved_accounts() else None
                lines.append(f"{'🟢' if who else '🔴'} {n}: " + (f"live, {_hours(who.get('expiry_seconds'))} left"
                                                                 if who else "no live session"))
            self.tg.send(chat, "\n".join(lines) or "no accounts")
        elif cmd == "/login":
            targets = [arg.strip()] if arg.strip() else mine
            for n in targets:
                if n not in self.cfg["accounts"] or not self.allowed(chat, n):
                    self.tg.send(chat, f"unknown account {n}")
                    continue
                threading.Thread(target=self.login, args=(n, "requested"), daemon=True).start()
        else:
            self.tg.send(chat, "Commands: /status, /login [account], /id")

    def run(self, tick_seconds: int = 300) -> None:
        def scheduler():
            while True:
                try:
                    self.tick()
                except Exception as e:                 # keep the daemon alive
                    print(f"scheduler error: {e}")
                self.sleep(tick_seconds)

        threading.Thread(target=scheduler, daemon=True).start()
        offset = None
        print(f"telegram broker running for {', '.join(self.cfg['accounts'])}")
        while True:
            try:
                for u in self.tg.updates(offset):
                    offset = u["update_id"] + 1
                    if "message" in u:
                        self.handle(u["message"])
            except Exception as e:
                print(f"telegram poll error: {e}")
                self.sleep(5)
