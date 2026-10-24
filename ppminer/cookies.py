"""BRAIN session cookies per account, same flow as the old Alpha Studio:
log in (face scan) in Chrome, then read the session cookie from Chrome's
cookie store and save it to data/secrets/<account>.cookie (mode 600).

One Chrome profile per BRAIN account lets several accounts stay logged in;
each profile's session is checked against BRAIN to learn which user it is.
Cookie values are never printed.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile

import requests

from .brain import BASE

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRETS = os.path.join(ROOT, "data", "secrets")
ACCOUNTS = os.path.join(SECRETS, "accounts.json")       # {"acc1": "<brain user id>", ...}
CHROME_ROOT = os.path.expanduser("~/Library/Application Support/Google/Chrome")
BRAIN_HOSTS = ("'.worldquantbrain.com'", "'worldquantbrain.com'", "'api.worldquantbrain.com'",
               "'.api.worldquantbrain.com'", "'platform.worldquantbrain.com'", "'.platform.worldquantbrain.com'")


# ── storage ─────────────────────────────────────────────────────────────────
def _ensure_dir():
    os.makedirs(SECRETS, exist_ok=True)
    os.chmod(SECRETS, 0o700)


def cookie_path(account: str) -> str:
    return os.path.join(SECRETS, f"{account}.cookie")


def save_cookie(account: str, cookie: str) -> str:
    _ensure_dir()
    path = cookie_path(account)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(cookie.strip())
    os.chmod(path, 0o600)
    return path


def load_cookie(account: str) -> str:
    with open(cookie_path(account)) as f:
        return f.read().strip()


def accounts() -> dict[str, str]:
    """alias -> BRAIN user id (optional mapping kept by the user)."""
    if not os.path.exists(ACCOUNTS):
        return {}
    with open(ACCOUNTS) as f:
        return json.load(f)


def alias_for(user_id: str) -> str:
    for alias, uid in accounts().items():
        if uid == user_id:
            return alias
    return user_id


def saved_accounts() -> list[str]:
    if not os.path.isdir(SECRETS):
        return []
    return sorted(f[:-7] for f in os.listdir(SECRETS) if f.endswith(".cookie"))


# ── session check ───────────────────────────────────────────────────────────
def only_session(cookie: str) -> str:
    """Keep just the `t` session cookie; analytics/zendesk cookies only add noise."""
    for part in cookie.split(";"):
        k, _, v = part.strip().partition("=")
        if k == "t" and v:
            return f"t={v}"
    return cookie.strip()


def whoami(cookie: str) -> dict | None:
    """{'user_id', 'expiry_seconds'} if the cookie is a live BRAIN session, else None."""
    s = requests.Session()
    s.headers["Cookie"] = only_session(cookie)
    try:
        r = s.get(f"{BASE}/authentication", timeout=30)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    d = r.json() if r.text.strip() else {}
    return {"user_id": (d.get("user") or {}).get("id"), "expiry_seconds": (d.get("token") or {}).get("expiry")}


# ── Chrome ──────────────────────────────────────────────────────────────────
def _chrome_secret() -> str:
    res = subprocess.run(["security", "find-generic-password", "-s", "Chrome Safe Storage", "-a", "Chrome", "-w"],
                         capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise RuntimeError("could not read Chrome Safe Storage from the keychain (allow access when macOS asks)")
    return res.stdout.strip()


def _decrypt(encrypted: bytes, secret: str) -> str:
    if not encrypted.startswith(b"v10"):
        return encrypted.decode("utf-8", "ignore")
    key = hashlib.pbkdf2_hmac("sha1", secret.encode(), b"saltysalt", 1003, dklen=16)
    p = subprocess.run(["openssl", "enc", "-aes-128-cbc", "-d", "-K", key.hex(), "-iv", (b" " * 16).hex(), "-nopad"],
                       input=bytes(encrypted[3:]), capture_output=True, timeout=10)
    if p.returncode != 0:
        return ""
    out = p.stdout
    if out and 0 < out[-1] <= 16 and out.endswith(bytes([out[-1]]) * out[-1]):
        out = out[:-out[-1]]
    if len(out) > 32 and out[32:].startswith(b"eyJ"):   # newer Chrome: 32-byte domain-binding prefix
        out = out[32:]
    return out.decode("utf-8", "ignore")


def chrome_profiles() -> list[str]:
    if not os.path.isdir(CHROME_ROOT):
        return []
    return sorted(p for p in os.listdir(CHROME_ROOT)
                  if (p == "Default" or p.startswith("Profile ")) and os.path.exists(os.path.join(CHROME_ROOT, p, "Cookies")))


def chrome_sessions() -> list[dict]:
    """Every Chrome profile holding a live BRAIN session: [{profile, user_id, expiry_seconds, cookie}]."""
    secret = _chrome_secret()
    found = []
    for profile in chrome_profiles():
        tmp = tempfile.mkdtemp(prefix="brain-cookie-")
        try:
            db = os.path.join(tmp, "Cookies")
            shutil.copy2(os.path.join(CHROME_ROOT, profile, "Cookies"), db)
            c = sqlite3.connect(db)
            rows = c.execute(f"select name, value, encrypted_value from cookies "
                             f"where host_key in ({','.join(BRAIN_HOSTS)}) and name='t'").fetchall()
            c.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        for name, value, enc in rows:
            val = value or (_decrypt(enc, secret) if enc else "")
            if not val:
                continue
            who = whoami(f"t={val}")
            if who:
                found.append({"profile": profile, "cookie": f"t={val}", **who})
                break
    return found
