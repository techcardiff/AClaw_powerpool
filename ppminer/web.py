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
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import cookies as ck, sandbox
from .verdict import regular_verdict, verdict
from .theme import current as current_theme, theme_names

HERE = os.path.dirname(os.path.abspath(__file__))
SONG_PORT = int(os.getenv("PP_SONG_PORT", "5071"))      # Song's sandbox dashboard, shown under /song/
BANNER = (f'<div style="background:#fde68a;color:#222;padding:6px 16px">Sandbox <b>{sandbox.NAME}</b>: its own '
          'code and database; simulates on BRAIN but never submits. <a href="/">live dashboard</a></div>')


def _page(name: str) -> bytes:
    html = open(os.path.join(HERE, name), encoding="utf-8").read()
    return html.replace("<!--sandbox-->", BANNER if sandbox.ON else "").encode()


def _rows(conn, q, args=()):
    return [dict(r) for r in conn.execute(q, args)]


def snapshot(db_path: str) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    out = {
        "status": dict(conn.execute("select status, count(*) from candidates group by 1").fetchall()),
        "resims": _rows(conn, """select r.id, r.candidate_id, r.account, r.expr, r.alpha_id, r.sharpe, r.fitness,
                                        r.turnover, r.pp_corr, r.status, r.error, r.checks, r.created_at,
                                        r.lane, r.purpose, r.settings, c.sharpe as old_sharpe, c.datasets
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
    try:
        theme = current_theme(conn)
    except Exception:
        theme = None
    conn.close()
    out["regular"] = []
    for r in out["resims"]:
        if r["status"] == "done":
            r["verdict"], r["reasons"] = verdict(r["sharpe"], r["checks"])
            r["reg_verdict"], r["reg_fails"] = regular_verdict(r["checks"])
            if r["reg_verdict"] in ("reg_ok", "near", "pending"):
                out["regular"].append({k: r[k] for k in ("alpha_id", "candidate_id", "account", "expr", "sharpe", "fitness",
                                                         "turnover", "lane", "purpose", "reg_verdict", "reg_fails", "datasets")})
            st = json.loads(r.get("settings") or "{}")
            r["themes"] = theme_names(r["checks"], theme, st.get("region"), st.get("delay"))   # the rule: no description yet
        r.pop("checks", None)
        r.pop("settings", None)
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


def _q(path: str) -> dict:
    from urllib.parse import parse_qs, urlparse
    return {k: v[0] for k, v in parse_qs(urlparse(path).query).items()}


def search_get(db_path: str, path: str) -> dict:
    from . import autosubmit, search
    q = _q(path)
    route = path.split("?")[0]
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        acct, region = q.get("account", ""), q.get("region", "USA")
        if route == "/api/search/meta":
            cfg = autosubmit.load_config()
            return {"accounts": [a for a, c in cfg.get("accounts", {}).items() if c.get("enabled")],
                    "regions": cfg.get("regions", ["USA"])}
        if route == "/api/search/funnel":
            return search.funnel(conn, acct, q.get("region") or None)
        if route == "/api/search/map":
            return {"rows": search.dataset_map(conn, acct, region)}
        if route == "/api/search/jobs":
            return {"jobs": search.manual_jobs(conn, acct)}
        if route == "/api/search/datasets":
            return {"datasets": search.datasets(conn, acct, region)}
        if route == "/api/search/fields":
            return search.fields(conn, acct, region, q.get("dataset") or None, q.get("category") or None,
                                 q.get("type") or None, float(q.get("min_cov") or 0), q.get("q") or None,
                                 q.get("sort") or "alphas")
        return {"error": f"unknown route {route}"}
    finally:
        conn.close()


def search_post(db_path: str, path: str, b: dict) -> dict:
    from . import search
    from .db import connect
    conn = connect(db_path)
    try:
        acct, region = b.get("account", ""), b.get("region", "USA")
        if path == "/api/search/check":
            return {"ok": True, **search.check_expr(conn, acct, region, b["expr"])}
        if path == "/api/search/simulate":
            rid = search.queue_manual(conn, acct, region, b["expr"], b.get("settings"))
            return {"ok": True, "job": rid}
        if path in ("/api/search/templates/preview", "/api/search/templates/queue"):
            args = (conn, acct, region, b["kind"], b.get("dataset"), int(b.get("n_fields") or 6), b.get("base_expr"),
                    b.get("order") or "coverage", b.get("fields") or None)
            if path.endswith("preview"):
                rows = search.template_preview(*args)
                return {"ok": True, "rows": rows, "valid": sum(not r["problems"] for r in rows)}
            return {"ok": True, "queued": search.template_queue(*args)}
        return {"ok": False, "error": f"unknown route {path}"}
    finally:
        conn.close()


def serve(db_path: str, port: int = 5070) -> None:
    class H(BaseHTTPRequestHandler):
        def _send(self, body: bytes, ctype: str = "application/json", code: int = 200):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _proxy(self, body: bytes | None = None):
            """/song/... -> Song's sandbox dashboard, so it shares this site and its password."""
            if self.path == "/song":
                self.send_response(301)
                self.send_header("Location", "/song/")
                self.end_headers()
                return
            req = urllib.request.Request(f"http://127.0.0.1:{SONG_PORT}{self.path[len('/song'):]}", data=body,
                                         method=self.command)
            for h in ("Content-Type", "X-PP-Dashboard"):
                if self.headers.get(h):
                    req.add_header(h, self.headers[h])
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    self._send(r.read(), r.headers.get("Content-Type", "text/plain"), r.status)
            except urllib.error.HTTPError as e:
                self._send(e.read(), e.headers.get("Content-Type", "text/plain"), e.code)
            except OSError:
                self._send(b"Song's sandbox is not running. On the 3080: bash /data/aclaw_song/scripts/sandbox.sh start",
                           "text/plain; charset=utf-8", 502)

        def _is_song(self) -> bool:
            return not sandbox.ON and (self.path == "/song" or self.path.startswith("/song/"))

        def do_GET(self):
            if self._is_song():
                self._proxy()
            elif self.path.startswith("/api/state"):
                self._send(json.dumps(snapshot(db_path), default=str).encode())
            elif self.path.startswith("/api/login-status"):
                self._send(json.dumps(login_status("force" in self.path)).encode())
            elif self.path in ("/", "/index.html"):
                self._send(_page("web.html"), "text/html; charset=utf-8")
            elif self.path == "/search":
                self._send(_page("search.html"), "text/html; charset=utf-8")
            elif self.path.startswith("/api/search/"):
                self._send(json.dumps(search_get(db_path, self.path), default=str).encode())
            else:
                self.send_error(404)

        def do_POST(self):
            if self._is_song():
                self._proxy(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                return
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
                elif self.path.startswith("/api/search/"):
                    out = search_post(db_path, self.path, body)
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
