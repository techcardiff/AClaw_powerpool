"""Background correlation fetch for alphas the submit pass keeps skipping.

24 Nov: YO50689's best alphas (five ASI D1 Power Pool picks, Sharpe 2.0-3.1, and two regular ones at
Sharpe 5.33) were skipped for up to a week with "self-correlation still computing" / "SELF_CORRELATION
PENDING": the pass waits 60 s per alpha and BRAIN never finished in that time. BRAIN refuses a submission
while its own check still shows self-correlation PENDING (1YNk2wwX, 3 Nov), so a value we compute can't
stand in for it. What helps is asking BRAIN's correlation endpoints for long enough that it finishes:
on 21 Nov a 90 s request got production correlations that 300 s waits during the pass had not.

So, between passes, one alpha at a time: the alphas skipped for pending or unknown correlation in the
last WINDOW_DAYS (best Sharpe first, not already submitted, not tried in the last RETRY_HOURS) get up to
WAIT seconds on /correlations/self and /correlations/prod. Answers are kept in corr_warm (and production
ones in prod_corr_seen, which the daily pick uses), and when any came back the account's submit pass is
made due, so it reads BRAIN's check while the result is fresh.
"""
from __future__ import annotations

import time

from .brain import BASE, AuthRequired

WAIT = 8 * 60
WINDOW_DAYS = 3
RETRY_HOURS = 12
PER_ROUND = 6
EVERY = 20 * 60

SCHEMA = """
create table if not exists corr_warm (
    account text, alpha_id text, kind text, value real, status text, seconds real,
    at timestamp default current_timestamp, primary key (account, alpha_id, kind));
create table if not exists prod_corr_seen (
    alpha_id text primary key, value real, seen_at timestamp default current_timestamp);
"""

STUCK = ("self-correlation still computing%", "%SELF_CORRELATION PENDING%", "%PROD_CORRELATION PENDING%",
         "production correlation unknown%", "%correlations/self%")


def targets(conn, account: str, n: int = PER_ROUND) -> list[str]:
    conn.executescript(SCHEMA)
    like = " or ".join("detail like ?" for _ in STUCK)
    ids = [a for (a,) in conn.execute(
        f"""select distinct alpha_id from autosubmit_log where account=? and alpha_id is not null
            and action in ('skip', 'regular_skip') and ts >= datetime('now', ?) and ({like})""",
        (account, f"-{WINDOW_DAYS} days", *STUCK))]
    done = {a for (a,) in conn.execute("select alpha_id from account_submitted where account=?", (account,))}
    recent = {a for (a,) in conn.execute("select alpha_id from corr_warm where account=? and at >= datetime('now', ?)",
                                         (account, f"-{RETRY_HOURS} hours"))}

    def sharpe(a):
        r = conn.execute("select max(sharpe) from resims where alpha_id=?", (a,)).fetchone()
        q = conn.execute("select max(sharpe) from regular_queue where alpha_id=?", (a,)).fetchone() \
            if conn.execute("select 1 from sqlite_master where name='regular_queue'").fetchone() else (None,)
        return max(x for x in (r[0], q[0], 0) if x is not None)

    return sorted((a for a in ids if a not in done and a not in recent), key=sharpe, reverse=True)[:n]


def _value(kind: str, d: dict):
    if d.get("max") is not None:
        return d["max"]
    if kind == "self":                       # top 5 records; none = nothing submitted correlates
        try:
            names = [p["name"] for p in d["schema"]["properties"]]
            i = names.index("correlation")
            vals = [abs(r[i]) for r in d.get("records", []) if r[i] is not None]
            return max(vals) if vals else 0.0
        except (KeyError, ValueError, TypeError):
            return None
    return None


def warm(conn, brain, account: str, alpha_id: str, wait: int = WAIT, clock=time.time) -> dict:
    """Ask BRAIN for both correlations of one alpha; returns {kind: value or None}."""
    conn.executescript(SCHEMA)
    out = {}
    for kind in ("self", "prod"):
        t = clock()
        try:
            v = _value(kind, brain._poll(f"{BASE}/alphas/{alpha_id}/correlations/{kind}", max_wait=wait))
            status = "answered" if v is not None else "empty"
        except AuthRequired:
            raise
        except Exception as e:
            v, status = None, "timeout" if isinstance(e, TimeoutError) else f"error: {str(e)[:80]}"
        conn.execute("insert or replace into corr_warm(account, alpha_id, kind, value, status, seconds) values (?,?,?,?,?,?)",
                     (account, alpha_id, kind, v, status, round(clock() - t)))
        if kind == "prod" and v is not None:
            conn.execute("insert or replace into prod_corr_seen(alpha_id, value, seen_at) values (?,?,current_timestamp)",
                         (alpha_id, v))
        conn.commit()
        out[kind] = v
    return out


def round_for(conn, brain, account: str, log=print, wait: int = WAIT) -> int:
    """One round for one account; returns how many alphas got an answer. Makes the submit pass due if any did."""
    got = 0
    for aid in targets(conn, account):
        r = warm(conn, brain, account, aid, wait)
        if any(v is not None for v in r.values()):
            got += 1
        log(f"[{account}] correlations for {aid}: self {r['self']}, prod {r['prod']}"
            + ("" if any(v is not None for v in r.values()) else f" (BRAIN gave nothing in {wait // 60} min each)"))
    if got:
        conn.execute("delete from runner_state where key=?", (f"autosubmit:{account}",))
        conn.commit()
        log(f"[{account}] {got} alpha(s) got correlations: submit pass made due")
    return got


def loop(db_path: str, accounts: list[str], log=print, sleep=time.sleep) -> None:
    """One round over the accounts with a live session, then a pause; the scheduler restarts it."""
    from .db import connect
    from .runner import _brain
    conn = connect(db_path)
    try:
        for account in accounts:
            b = _brain(account)
            if b is None:
                continue
            try:
                round_for(conn, b, account, log)
            except AuthRequired:
                continue
            except Exception as e:
                log(f"[{account}] correlation fetch: {e}")
    finally:
        conn.close()
    sleep(EVERY)
