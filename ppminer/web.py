"""Local progress dashboard: python -m ppminer web  ->  http://127.0.0.1:5070

Read-only view of data/pp.db: pipeline counts, re-simulations (live), the
current shortlist with descriptions, and recent Qwen rewrites.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import cookies as ck
from .verdict import regular_verdict, verdict

HERE = os.path.dirname(os.path.abspath(__file__))


def _rows(conn, q, args=()):
    return [dict(r) for r in conn.execute(q, args)]


def snapshot(db_path: str) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    out = {
        "status": dict(conn.execute("select status, count(*) from candidates group by 1").fetchall()),
        "resims": _rows(conn, """select r.id, r.candidate_id, r.account, r.expr, r.alpha_id, r.sharpe, r.fitness,
                                        r.turnover, r.pp_corr, r.status, r.error, r.checks, r.created_at,
                                        r.lane, r.purpose, c.sharpe as old_sharpe, c.datasets
                                 from resims r join candidates c on c.id = r.candidate_id order by r.id desc limit 200"""),
        "described": _rows(conn, """select d.candidate_id, d.text, c.expr, c.sharpe, c.datasets, c.n_ops
                                    from descriptions d join candidates c on c.id = d.candidate_id
                                    order by abs(c.sharpe) desc limit 100"""),
        "rewrites": _rows(conn, """select w.parent_id, w.target_category, w.expr, w.valid, w.problems, w.created_at
                                   from rewrites w order by w.id desc limit 60"""),
        "rewrite_counts": dict(zip(("valid", "total"), conn.execute(
            "select coalesce(sum(valid),0), count(*) from rewrites").fetchone())),
    }
    try:   # created by `picklist`
        out["picks"] = _rows(conn, """select p.alpha_id, p.candidate_id, p.account, p.score, p.max_corr, p.corr_with,
                                             p.members, p.status, p.decided_at, r.expr, r.sharpe, r.fitness, r.turnover,
                                             c.datasets, c.categories, c.single_dataset, c.n_ops, d.text as description
                                      from picks p join resims r on r.id = p.resim_id
                                      join candidates c on c.id = p.candidate_id
                                      left join descriptions d on d.candidate_id = p.candidate_id
                                      order by case p.status when 'approved' then 0 when 'proposed' then 1
                                               when 'submitted' then 2 else 3 end, p.score desc""")
    except sqlite3.OperationalError:
        out["picks"] = []
    try:   # tables created by fields-sync; absent until the first sync
        out["field_sync"] = _rows(conn, """select account, region, delay, count(*) as fields, count(distinct dataset) as datasets,
                                                  (select count(*) from account_datasets d where d.account=f.account
                                                     and d.region=f.region and d.delay=f.delay) as datasets_total
                                           from account_fields f group by account, region, delay""")
        out["blocked"] = _rows(conn, "select account, field, reason from field_blocklist order by created_at desc limit 20")
    except sqlite3.OperationalError:
        out["field_sync"], out["blocked"] = [], []
    try:   # written by `run` (runner.py)
        out["runner"] = {k: json.loads(v) for k, v in conn.execute("select key, value from runner_state")}
        out["runner"]["updated_at"] = conn.execute("select max(updated_at) from runner_state").fetchone()[0]
    except sqlite3.OperationalError:
        out["runner"] = {}
    try:   # written by `autosubmit` / `daily`
        out["autosubmit"] = _rows(conn, """select ts, account, ny_date, alpha_id, action, detail from autosubmit_log
                                           order by id desc limit 60""")
    except sqlite3.OperationalError:
        out["autosubmit"] = []
    conn.close()
    out["regular"] = []
    for r in out["resims"]:
        if r["status"] == "done":
            r["verdict"], r["reasons"] = verdict(r["sharpe"], r["checks"])
            r["reg_verdict"], r["reg_fails"] = regular_verdict(r["checks"])
            if r["reg_verdict"] in ("reg_ok", "near", "pending"):
                out["regular"].append({k: r[k] for k in ("alpha_id", "candidate_id", "account", "expr", "sharpe", "fitness",
                                                         "turnover", "lane", "purpose", "reg_verdict", "reg_fails", "datasets")})
            ch = {c.get("name"): c for c in json.loads(r["checks"] or "[]") or []}
            r["themes"] = [t["name"] for t in (ch.get("MATCHES_THEMES") or {}).get("themes", [])
                           if "power pool" in t["name"].lower()]
        r.pop("checks", None)
    out["version"] = SERVER_VERSION
    return out


LOGIN_URL = "https://platform.worldquantbrain.com/sign-in"
SERVER_VERSION = str(int(time.time()))   # pages reload when this changes (server restarted with new code)
_status_cache: dict = {"at": 0.0, "data": None}


def login_status(force: bool = False) -> dict:
    """Saved sessions (live/expired) + Chrome profiles. Cached 60 s so the page
    does not hit BRAIN's /authentication on every refresh."""
    if not force and _status_cache["data"] and time.time() - _status_cache["at"] < 60:
        return _status_cache["data"]
    accts = []
    for name in ck.saved_accounts():
        who = ck.whoami(ck.load_cookie(name))
        accts.append({"name": name, "live": bool(who), "user_id": (who or {}).get("user_id"),
                      "hours_left": round(who["expiry_seconds"] / 3600, 1) if who and who.get("expiry_seconds") else None})
    data = {"accounts": accts, "profiles": ck.chrome_profiles(), "telegram": telegram_accounts(),
            "checked_at": time.strftime("%H:%M:%S")}
    _status_cache.update(at=time.time(), data=data)
    return data


def open_login(profile: str) -> None:
    if profile not in ck.chrome_profiles():
        raise ValueError(f"unknown Chrome profile {profile!r}")
    subprocess.run(["open", "-na", "Google Chrome", "--args", f"--profile-directory={profile}", LOGIN_URL],
                   check=True, timeout=20)


def grab() -> list[dict]:
    saved = []
    for s in ck.chrome_sessions():
        name = ck.alias_for(s["user_id"])
        ck.save_cookie(name, s["cookie"])
        saved.append({"name": name, "profile": s["profile"], "user_id": s["user_id"]})
    login_status(force=True)
    return saved


_broker = None


def telegram_accounts() -> list[str] | str:
    """Accounts set up for Telegram login, or the reason it isn't set up."""
    from .telegram import CONFIG, ConfigError, load_config
    if not os.path.exists(CONFIG) and os.getenv("PP_BROKER_ELSEWHERE"):
        return ("the Telegram broker runs on the Mac (Telegram is blocked from this server). "
                "Send /login to the bot, or /status to see sessions.")
    try:
        return list(load_config()["accounts"])
    except ConfigError as e:
        return str(e)


def telegram_login(account: str) -> dict:
    """Send the face-scan link for `account` to its owner's Telegram (runs in the background)."""
    global _broker
    import threading
    from .telegram import Broker, load_config
    if _broker is None:
        _broker = Broker(load_config())
    if account not in _broker.cfg["accounts"]:
        return {"ok": False, "error": f"{account} is not in telegram.json"}
    threading.Thread(target=lambda: (_broker.login(account, "requested from dashboard"), login_status(force=True)),
                     daemon=True).start()
    return {"ok": True}


def serve(db_path: str, port: int = 5070) -> None:
    page = open(os.path.join(HERE, "web.html"), "rb").read()

    class H(BaseHTTPRequestHandler):
        def _send(self, body: bytes, ctype: str = "application/json", code: int = 200):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/api/state"):
                self._send(json.dumps(snapshot(db_path), default=str).encode())
            elif self.path.startswith("/api/login-status"):
                self._send(json.dumps(login_status("force" in self.path)).encode())
            elif self.path in ("/", "/index.html"):
                self._send(page, "text/html; charset=utf-8")
            else:
                self.send_error(404)

        def do_POST(self):
            # only our own page sends this header; blocks cross-site form posts to localhost
            if self.headers.get("X-PP-Dashboard") != "1":
                self.send_error(403)
                return
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            try:
                if self.path == "/api/login/open":
                    open_login(body.get("profile", ""))
                    out = {"ok": True}
                elif self.path == "/api/login/grab":
                    out = {"ok": True, "saved": grab()}
                elif self.path == "/api/login/telegram":
                    out = telegram_login(body.get("account", ""))
                elif self.path == "/api/pick/decide":
                    from .picks import decide
                    conn = sqlite3.connect(db_path, timeout=30)
                    decide(conn, body["alpha_id"], body["status"], body.get("note"))
                    conn.close()
                    out = {"ok": True}
                else:
                    self.send_error(404)
                    return
            except Exception as e:
                out = {"ok": False, "error": str(e)}
            self._send(json.dumps(out).encode())

        def log_message(self, *a):
            pass

    print(f"dashboard on http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
