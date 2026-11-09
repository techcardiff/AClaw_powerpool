"""Regular (consultant) alpha submissions from a reviewed queue. Runs inside autosubmit, after
the account's Power Pool submissions, so it is still the single submitter.

The queue (table regular_queue) is filled by `python -m ppminer regular-queue add FILE` from
a reviewed list: one alpha per correlation group, each on the account whose research it is.
Per run and account (config: accounts.<id>.regular in data/autosubmit.json):
  * room = min(per_day - regular submissions today, BRAIN's daily limit - all submissions today)
  * best first, each alpha gets a fresh BRAIN check; it must have no failing check except the
    Power Pool / theme / competition ones, self- and production correlation computed and PASS
  * dry_run (the default) logs what it would submit and stops there
An alpha whose correlation fails stays queued: earlier submissions (ours or the team's) are
what it collides with, so it is rechecked on later runs until it is marked 'dropped' by hand.
"""
from __future__ import annotations

import json
import sqlite3

from . import cookies as ck
from .brain import Brain
from .quota import DAILY_SUBMISSION_LIMIT, ny_date, ny_now

SCHEMA = """
create table if not exists regular_queue (
    account text, alpha_id text, expr text, settings text, origin text, note text,
    sharpe real, fitness real, turnover real, status text default 'queued',
    checked text, detail text, updated_at timestamp default current_timestamp,
    primary key (account, alpha_id));
"""
# checks that don't decide a regular submission
NOT_REGULAR = {"MATCHES_THEMES", "MATCHES_COMPETITION", "MATCHES_PYRAMID", "POWER_POOL_CORRELATION",
               "OSMOSIS_ALLOCATION", "CLUSTER_TEST", "DATA_DIVERSITY", "REGULAR_SUBMISSION"}
MUST_PASS = ("SELF_CORRELATION", "PROD_CORRELATION")


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def add(conn: sqlite3.Connection, rows: list[dict]) -> int:
    migrate(conn)
    n = 0
    for r in rows:
        n += conn.execute("""insert or ignore into regular_queue(account, alpha_id, expr, settings, origin, note, sharpe, fitness, turnover)
                             values (?,?,?,?,?,?,?,?,?)""",
                          (r["account"], r["alpha_id"], r.get("expr"), json.dumps(r.get("settings") or {}), r.get("origin"),
                           r.get("note"), r.get("sharpe"), r.get("fitness"), r.get("turnover"))).rowcount
    conn.commit()
    return n


def judge(checks: list[dict]) -> tuple[bool, str]:
    """(ok, reason) for a regular submission from BRAIN's /check items."""
    ch = {c.get("name"): c for c in checks or []}
    fails = sorted(n for n, c in ch.items() if c.get("result") == "FAIL" and n not in NOT_REGULAR)
    if fails:
        return False, "fails " + ", ".join(f"{n}={ch[n].get('value')}" for n in fails)
    for n in MUST_PASS:
        res = (ch.get(n) or {}).get("result")
        if res != "PASS":
            return False, f"{n} {res or 'missing'}"
    return True, ""


def today_counts(conn: sqlite3.Connection, account: str, today=None) -> tuple[int, int]:
    """(all submissions today, regular ones today) in New York days, from account_submitted."""
    today = today or ny_now().date()
    total = regular = 0
    for tags, date in conn.execute("select tags, date_submitted from account_submitted where account=?", (account,)):
        if ny_date(date) == today:
            total += 1
            regular += "PowerPoolSelected" not in json.loads(tags or "[]")
    return total, regular


def run_account(conn: sqlite3.Connection, account: str, dry_run: bool, per_day: int = 2, brain: Brain | None = None,
                log=None) -> dict:
    from .runner import checked
    migrate(conn)
    log = log or (lambda *a, **k: None)
    if brain is None:
        brain = Brain.from_cookie(ck.load_cookie(account)) if account in ck.saved_accounts() else None
    if brain is None or not brain.is_authenticated():
        return {"status": "waiting_login"}
    total, regular = today_counts(conn, account)
    room = max(0, min(per_day - regular, DAILY_SUBMISSION_LIMIT - total))
    if room == 0:
        log("regular_quota", detail=f"{regular}/{per_day} regular, {total}/{DAILY_SUBMISSION_LIMIT} in all today")
        return {"status": "quota_full", "submitted": 0}
    submitted = 0
    for q in conn.execute("""select * from regular_queue where account=? and status='queued'
                             order by fitness desc""", (account,)).fetchall():
        if submitted >= room:
            break
        aid = q["alpha_id"]
        s = checked(brain, brain.alpha(aid))
        if "checks" not in s:
            log("regular_skip", aid, "BRAIN check still running; next run")
            continue
        ok, why = judge(s["checks"])
        conn.execute("update regular_queue set checked=current_timestamp, detail=?, sharpe=?, fitness=?, turnover=? where account=? and alpha_id=?",
                     (why or "ok", s.get("sharpe"), s.get("fitness"), s.get("turnover"), account, aid))
        conn.commit()
        if not ok:
            log("regular_skip", aid, why)
            continue
        if dry_run:
            log("regular_dry_run", aid, f"would submit as regular: sharpe {s.get('sharpe')} fitness {s.get('fitness')}")
            submitted += 1
            continue
        res = brain.submit(aid)
        if not res.get("submitted"):
            log("regular_submit_failed", aid, res)
            conn.execute("update regular_queue set detail=? where account=? and alpha_id=?",
                         (f"refused: {res.get('refused')}", account, aid))
            conn.commit()
            continue
        conn.execute("update regular_queue set status='submitted', detail=?, updated_at=current_timestamp where account=? and alpha_id=?",
                     (f"{res.get('dateSubmitted')} tags {res.get('tags')}", account, aid))
        conn.commit()
        log("regular_submitted", aid, f"sharpe {s.get('sharpe')} fitness {s.get('fitness')} tags {res.get('tags')}")
        submitted += 1
    return {"status": "done", "submitted": submitted, "room": room}
