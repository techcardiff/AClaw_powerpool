"""Daily Telegram report: what each account submitted on a BRAIN day, and how simulations went.

The Telegram broker sends it once a day, just after BRAIN's day ends (00:05 New York time, 13:05 CST in
winter), for the day that just ended, to `report.chat_ids` in telegram.json (default: admin_chat_ids;
a channel works too, with the bot added to it as an admin). `/report` in the bot sends it on demand,
`/report today` for the day so far. It reads data/pp.db and never calls BRAIN except to say whether each
session is live, so it needs the broker on the same machine as the submitter's database.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3

from .quota import NY, ny_date, ny_now

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.getenv("PP_DB", os.path.join(ROOT, "data", "pp.db"))
SENT_FILE = os.path.join(ROOT, "data", "report_sent.txt")
LANE = {"submitted": "Power Pool", "regular_submitted": "regular", "super_submitted": "super"}


def _utc_bounds(day: dt.date) -> tuple[str, str]:
    """The New York day as UTC 'YYYY-MM-DD HH:MM:SS' bounds (sqlite current_timestamp is UTC)."""
    a = dt.datetime.combine(day, dt.time(0), NY).astimezone(dt.timezone.utc)
    b = a + dt.timedelta(days=1)
    f = "%Y-%m-%d %H:%M:%S"
    return a.strftime(f), b.strftime(f)


def _has(conn, table: str) -> bool:
    return bool(conn.execute("select 1 from sqlite_master where name=?", (table,)).fetchone())


def _scope(conn, account: str, aid: str) -> str:
    """'GLB D1' from whichever table simulated the alpha."""
    for sql in ("select settings from resims where alpha_id=? limit 1",
                "select settings from regular_queue where account=? and alpha_id=?",
                "select settings from super_alphas where account=? and alpha_id=?"):
        table = sql.split(" from ")[1].split()[0]
        if not _has(conn, table):
            continue
        row = conn.execute(sql, (aid,) if "resims" in sql else (account, aid)).fetchone()
        if row and row[0]:
            try:
                s = json.loads(row[0])
                return f"{s.get('region', '?')} D{s.get('delay', '?')}"
            except ValueError:
                pass
    return ""


def _field(detail: str, key: str) -> str | None:
    """'prod 0.589' -> '0.589' from an autosubmit_log detail."""
    words = (detail or "").replace("|", " ").split()
    for i, w in enumerate(words[:-1]):
        if w == key:
            return words[i + 1]
    return None


def submissions(conn, account: str, day: dt.date) -> list[dict]:
    """Everything BRAIN lists as submitted that New York day (manual ones too), with our log's details."""
    logged = {}
    for aid, action, detail in conn.execute(
            "select alpha_id, action, detail from autosubmit_log where account=? and ny_date=? and action in "
            "('submitted', 'regular_submitted', 'super_submitted') order by id", (account, str(day))):
        logged[aid] = (action, detail or "")
    out = {}
    if _has(conn, "account_submitted"):
        for aid, sharpe, tags, date in conn.execute(
                "select alpha_id, sharpe, tags, date_submitted from account_submitted where account=?", (account,)):
            if ny_date(date) == day:
                out[aid] = {"alpha_id": aid, "sharpe": sharpe, "tags": json.loads(tags or "[]")}
    for aid in logged:
        out.setdefault(aid, {"alpha_id": aid, "sharpe": None, "tags": []})
    supers = {a for (a,) in conn.execute("select alpha_id from super_alphas where account=?", (account,))} \
        if _has(conn, "super_alphas") else set()
    for aid, s in out.items():
        action, detail = logged.get(aid, ("", ""))
        s["lane"] = LANE.get(action) or ("super" if aid in supers else
                                         "Power Pool" if "PowerPoolSelected" in s["tags"] else "submitted by hand")
        s["sharpe"] = s["sharpe"] if s["sharpe"] is not None else _field(detail, "sharpe")
        s["prod"] = _field(detail, "prod")
        s["scope"] = _scope(conn, account, aid)
    return list(out.values())


def simulations(conn, account: str, day: dt.date) -> dict:
    a, b = _utc_bounds(day)
    r = conn.execute("select sum(status='done'), sum(status='error'), sum(status in ('queued','running')), "
                     "max(case when status='done' then sharpe end) from resims "
                     "where account=? and created_at>=? and created_at<?", (account, a, b)).fetchone()
    out = {"done": r[0] or 0, "error": r[1] or 0, "open": r[2] or 0, "best_sharpe": r[3]}
    out["picks"] = conn.execute("select count(*) from picks where account=? and status in ('proposed','approved')",
                                (account,)).fetchone()[0] if _has(conn, "picks") else 0
    out["regular"] = conn.execute("select count(*) from regular_queue where account=? and status='queued'",
                                  (account,)).fetchone()[0] if _has(conn, "regular_queue") else 0
    if _has(conn, "super_alphas"):
        out["super_new"] = conn.execute("select count(*) from super_alphas where account=? and created_at>=? "
                                        "and created_at<? and status!='failed'", (account, a, b)).fetchone()[0]
        out["super_waiting"] = conn.execute("select count(*) from super_alphas where account=? and status='simulated'",
                                            (account,)).fetchone()[0]
    # hours with at least one simulation: 23 Nov, YO50689 simulated in 3 of 24 hours (no session the rest);
    # waiting_login log lines undercounted that, they only appear while a submit pass is due
    out["active_h"] = conn.execute("select count(distinct strftime('%Y-%m-%d %H', created_at)) from resims "
                                   "where account=? and created_at>=? and created_at<?", (account, a, b)).fetchone()[0]
    return out


def why_none(conn, account: str, day: dt.date) -> str:
    """One line on why nothing went in, from the day's last decisive log entry."""
    row = conn.execute("select action, alpha_id, detail from autosubmit_log where account=? and ny_date=? and action in "
                       "('daily_candidates', 'daily_total', 'no_candidate', 'submit_failed') order by id desc limit 1",
                       (account, str(day))).fetchone()
    if not row:
        return "no alpha passed every check"
    action, aid, detail = row
    if action == "daily_candidates":
        n = len([x for x in (detail or "").split(",") if x.strip()])
        return f"{n} candidate(s) passed, none submitted ({(detail or '')[:120]})"
    if action == "submit_failed":
        return f"BRAIN refused {aid}"
    return (detail or action)[:120]


def build(conn, accounts: list[str], day: dt.date, session=None, partial: bool = False) -> str:
    """The report text. session(account) -> hours left (float), or None when logged out."""
    title = f"📊 BRAIN day {day:%a %d %b}" + (" (so far)" if partial else " (ended 13:00 CST)")
    lines = [title]
    for acc in accounts:
        lines.append("")
        head = f"{acc}"
        if session is not None:
            left = session(acc)
            head += f"  {'🟢 live, ' + format(left, '.1f') + 'h left' if left is not None else '🔴 logged out now'}"
        lines.append(head)
        subs = submissions(conn, acc, day)
        if subs:
            for s in subs:
                bits = [s["lane"], s["scope"], f"Sharpe {s['sharpe']}" if s["sharpe"] is not None else "",
                        f"prod corr {s['prod']}" if s["prod"] else ""]
                lines.append(f"✅ {s['alpha_id']}: " + " · ".join(b for b in bits if b))
        else:
            lines.append(f"➖ nothing submitted: {why_none(conn, acc, day)}")
        sim = simulations(conn, acc, day)
        best = f", best Sharpe {sim['best_sharpe']:.2f}" if sim["best_sharpe"] is not None else ""
        lines.append(f"🔬 {sim['done']:,} simulations done, {sim['error']} errors{best}"
                     + (f", {sim['open']} still running" if sim["open"] else ""))
        lines.append(f"📥 queued: {sim['picks']} Power Pool picks, {sim['regular']} regular alphas")
        if "super_new" in sim and (sim["super_new"] or sim["super_waiting"]):
            lines.append(f"🧩 super: {sim['super_new']} new variants, {sim['super_waiting']} waiting on BRAIN's check")
        if not partial and sim["active_h"] < 20:
            lines.append(f"⏱ simulated in only {sim['active_h']} of 24 hours (usually no live session the rest)")
    return "\n".join(lines)


def due_day(now: dt.datetime | None = None) -> dt.date:
    """The BRAIN day a report is due for: the one before today in New York, from 00:05 New York time."""
    now = now or ny_now()
    return now.date() - dt.timedelta(days=1) if now.time() >= dt.time(0, 5) else now.date() - dt.timedelta(days=2)


def last_sent() -> str:
    try:
        with open(SENT_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def mark_sent(day: dt.date) -> None:
    with open(SENT_FILE, "w") as f:
        f.write(str(day))


def connect() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=60)
