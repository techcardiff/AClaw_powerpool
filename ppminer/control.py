"""Telegram control panel for the miner (the broker's commands besides /login and /report).

Owners steer their own account, admins any account. Everything goes through what the `daily`
scheduler already reads every 15 s, so nothing needs a restart:
  * data/autosubmit.json  accounts.<id>.regions / scope_targets   (/regions, /focus)
  * runner_state brain_pause:<id>   mining pause, checked at each cycle   (/pause, /resume)
  * runner_state autosubmit:<id>    last submit pass; removing it makes a pass due   (/pass)
The scheduler stays the only submitter: /pass only makes its next pass come now, within the
same daily limits. Submission counts, dry run and the quality bar are not changeable from here.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import time

from . import autosubmit
from .quota import ny_now
from .report import DB_PATH

REGIONS = ("USA", "EUR", "ASI", "GLB", "CHN", "JPN", "KOR", "TWN", "HKG", "IND", "AMR")
HELP = """Commands (account can be left out if you own just one):
/status — sessions, simulations, pauses, focus, next submit pass
/queue [account] — best alphas waiting to be submitted
/report [today] — the daily report (admins)
/pause [account] [hours] — stop simulating (default 2 h)
/resume [account]
/regions [account] EUR GLB — mine only there (all = every configured region)
/focus [account] EUR/1 GLB/1 [days] — submit these scopes first (off = stop)
/pass [account] — run the submit pass now (same daily limits)
/login [account], /id"""


def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=60)
    c.row_factory = sqlite3.Row
    return c


def load_cfg() -> dict:
    return autosubmit.load_config(autosubmit.CONFIG)


def save_cfg(cfg: dict) -> None:
    """Atomic replace: the scheduler re-reads the file every 15 s and must never see half of it."""
    path = autosubmit.CONFIG
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    if os.path.exists(path):
        os.chmod(tmp, os.stat(path).st_mode & 0o777)
    os.replace(tmp, path)


def _state(conn, key: str):
    row = conn.execute("select value from runner_state where key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def _set_state(conn, key: str, value) -> None:
    conn.execute("insert into runner_state(key, value) values (?,?) on conflict(key) do update set "
                 "value=excluded.value, updated_at=current_timestamp", (key, json.dumps(value)))
    conn.commit()


def status(accounts: list[str], session=None) -> str:
    cfg, conn, out = load_cfg(), db(), []
    try:
        start = ny_now().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(dt.timezone.utc)
        since_day = start.strftime("%Y-%m-%d %H:%M:%S")
        since_hour = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        retry = float(cfg.get("retry_hours", 3))
        for acc in accounts:
            a = cfg.get("accounts", {}).get(acc, {})
            line = [f"{acc}"]
            if session is not None:
                left = session(acc)
                line[0] += f"  {'🟢 ' + format(left, '.1f') + 'h left' if left is not None else '🔴 logged out'}"
            hour = conn.execute("select count(*) from resims where account=? and created_at>=?", (acc, since_hour)).fetchone()[0]
            day = conn.execute("select count(*) from resims where account=? and created_at>=?", (acc, since_day)).fetchone()[0]
            line.append(f"🔬 {hour} simulations in the last hour, {day:,} today (cap {int(cfg.get('max_sims_per_day', 5000)):,})")
            pause = _state(conn, f"brain_pause:{acc}") or 0
            if pause > time.time():
                line.append(f"⏸ paused until {dt.datetime.fromtimestamp(pause):%H:%M} CST")
            if not a.get("enabled"):
                line.append("⛔ disabled in autosubmit.json")
            line.append(f"🌍 regions: {', '.join(a.get('regions') or cfg.get('regions', ['USA']))}")
            st = a.get("scope_targets") or {}
            if st.get("scopes") and str(ny_now().date()) <= st.get("until", ""):
                line.append(f"🎯 focus: {', '.join(st['scopes'])} until {st['until']}")
            last = _state(conn, f"autosubmit:{acc}")
            if last:
                nxt = dt.datetime.fromisoformat(last) + dt.timedelta(hours=retry)
                line.append(f"📤 last submit pass {dt.datetime.fromisoformat(last).astimezone():%d %b %H:%M}, "
                            f"next about {nxt.astimezone():%H:%M} CST")
            out.append("\n".join(line))
    finally:
        conn.close()
    return "\n\n".join(out)


def queue(acc: str, n: int = 5) -> str:
    conn = db()
    try:
        has_picks = conn.execute("select 1 from sqlite_master where name='picks'").fetchone()
        pp = [] if not has_picks else conn.execute("select p.alpha_id, r.sharpe, r.fitness, r.settings from picks p left join resims r on r.id=p.resim_id "
                          "where p.account=? and p.status in ('proposed','approved') order by p.score desc limit ?",
                          (acc, n)).fetchall()
        rg = conn.execute("select alpha_id, sharpe, fitness, settings from regular_queue where account=? and status='queued' "
                          "order by fitness desc limit ?", (acc, n)).fetchall()
    finally:
        conn.close()

    def row(r):
        try:
            s = json.loads(r["settings"] or "{}")
            scope = f"{s.get('region', '?')} D{s.get('delay', '?')}"
        except ValueError:
            scope = "?"
        return f"  {r['alpha_id']} {scope} Sharpe {r['sharpe'] if r['sharpe'] is not None else '?'} fitness {r['fitness'] if r['fitness'] is not None else '?'}"

    return "\n".join([f"📥 {acc} — best Power Pool picks:"] + [row(r) for r in pp] + ["  (none)"] * (not pp) +
                     ["Regular queue:"] + [row(r) for r in rg] + ["  (none)"] * (not rg) +
                     ["Each still needs a fresh BRAIN check; the scheduler submits the least correlated one a day."])


def pause(acc: str, hours: float) -> str:
    conn = db()
    try:
        until = time.time() + hours * 3600
        _set_state(conn, f"brain_pause:{acc}", until)
    finally:
        conn.close()
    return f"⏸ {acc}: simulations paused until {dt.datetime.fromtimestamp(until):%d %b %H:%M} CST (the submit pass still runs)."


def resume(acc: str) -> str:
    conn = db()
    try:
        conn.execute("delete from runner_state where key=?", (f"brain_pause:{acc}",))
        conn.commit()
    finally:
        conn.close()
    return f"▶️ {acc}: simulating again from the next cycle."


def regions(acc: str, names: list[str]) -> str:
    cfg = load_cfg()
    a = cfg["accounts"][acc]
    if [n.lower() for n in names] == ["all"]:
        a.pop("regions", None)
        msg = f"🌍 {acc}: mining every configured region ({', '.join(cfg.get('regions', ['USA']))})."
    else:
        bad = [n for n in names if n.upper() not in REGIONS]
        if bad or not names:
            return f"Unknown region {', '.join(bad) or '(none given)'}. Use: {', '.join(REGIONS)} or all."
        a["regions"] = [n.upper() for n in names]
        msg = f"🌍 {acc}: mining only {', '.join(a['regions'])} from the next cycle (the theme's region stays in)."
    save_cfg(cfg)
    return msg


def focus(acc: str, args: list[str]) -> str:
    cfg = load_cfg()
    a = cfg["accounts"][acc]
    if [x.lower() for x in args] == ["off"]:
        a.pop("scope_targets", None)
        save_cfg(cfg)
        return f"🎯 {acc}: no focus scopes; submissions ranked on correlation only."
    days = 7
    if args and args[-1].isdigit():
        days = int(args.pop())
    scopes = []
    for s in args:
        r, _, d = s.upper().partition("/")
        if r not in REGIONS or d not in ("0", "1"):
            return f"Scope {s} should look like EUR/1 (region/delay)."
        scopes.append(f"{r}/{d}")
    if not scopes:
        return "Give at least one scope, e.g. /focus EUR/1 GLB/1 7"
    until = str(ny_now().date() + dt.timedelta(days=days))
    a["scope_targets"] = {"scopes": scopes, "until": until}
    save_cfg(cfg)
    return f"🎯 {acc}: submit {', '.join(scopes)} first until {until}."


def run_pass(acc: str) -> str:
    conn = db()
    try:
        conn.execute("delete from runner_state where key=?", (f"autosubmit:{acc}",))
        conn.commit()
    finally:
        conn.close()
    return (f"📤 {acc}: submit pass starts within a minute (once the previous one has finished). "
            "Same limit: at most one regular or Power Pool alpha a day.")
