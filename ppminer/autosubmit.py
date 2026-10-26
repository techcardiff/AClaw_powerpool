"""Daily automatic Power Pool submission. The ONLY part of ppminer that submits.

Per enabled account (config: data/autosubmit.json, see autosubmit.example.json):
  1. sync the account's submitted alphas, work out today's quota (New York day, quota.py)
  2. refresh the pick list (recheck pending checks, pull PnL, cluster, describe)
  3. walk the picks best-first; each must pass, right before submitting:
       - a fresh BRAIN check: Power Pool verdict pp_ok, POWER_POOL_CORRELATION = PASS,
         theme match once the account is past week 6, daily submission limit not reached
       - a verified description (descheck.py); otherwise the pick is skipped
  4. set the description, submit, confirm (ACTIVE + PowerPoolSelected), record it
Fewer than the quota is fine: nothing weak is submitted to fill a slot.
dry_run: everything except setting the description and submitting.
A file lock makes sure only one submitter runs at a time.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sqlite3
import time

from . import cookies as ck, lanes
from .brain import Brain, summarize
from .catalog import Catalog
from .descheck import verified_description
from .quota import DAILY_SUBMISSION_LIMIT, ny_now, quota
from .verdict import QUALITY_DEFAULTS, quality_gate, verdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "data", "autosubmit.json")
LOCK = os.path.join(ROOT, "data", "autosubmit.lock")

SCHEMA = """
create table if not exists autosubmit_log (
    id integer primary key, ts timestamp default current_timestamp, account text, ny_date text,
    alpha_id text, candidate_id integer, action text, detail text);
"""


def load_config(path: str = CONFIG) -> dict:
    if not os.path.exists(path):
        return {"accounts": {}}
    with open(path) as f:
        return json.load(f)


def _log(conn, account, ny_date, action, alpha_id=None, candidate_id=None, detail="") -> None:
    conn.executescript(SCHEMA)
    conn.execute("insert into autosubmit_log(account, ny_date, alpha_id, candidate_id, action, detail) values (?,?,?,?,?,?)",
                 (account, str(ny_date), alpha_id, candidate_id, action, str(detail)[:2000]))
    conn.commit()
    print(time.strftime("%H:%M:%S"), f"[{account}] {action} {alpha_id or ''} {str(detail)[:200]}", flush=True)


@contextlib.contextmanager
def single_submitter():
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    with open(LOCK, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another autosubmit is running (data/autosubmit.lock)")
        yield


def _candidates(conn, account: str) -> list[sqlite3.Row]:
    """Proposed/approved picks, approved first, then best score."""
    return conn.execute("""
        select p.*, r.expr, r.settings, r.sharpe, c.region from picks p
        join resims r on r.id = p.resim_id join candidates c on c.id = p.candidate_id
        where p.account=? and p.status in ('approved', 'proposed')
        order by case p.status when 'approved' then 0 else 1 end, p.score desc""", (account,)).fetchall()


def run_account(conn, catalog: Catalog, account: str, dry_run: bool, quality: dict | None = None) -> dict:
    from .runner import after_batch
    q0 = None
    brain = Brain.from_cookie(ck.load_cookie(account)) if account in ck.saved_accounts() else None
    if brain is None or not brain.is_authenticated():
        _log(conn, account, ny_now().date(), "waiting_login", detail="session not live")
        return {"status": "waiting_login"}
    lanes.sync_submitted(conn, brain, account, catalog)
    q = q0 = quota(conn, account)
    _log(conn, account, q.today, "quota", detail=f"{q.phase}: {q.used_today}/{q.per_day} used today, "
         f"{q.remaining} left, theme required: {q.theme_required}")
    if q.remaining <= 0:
        return {"status": "quota_full", "submitted": 0}
    after_batch(conn, catalog, brain, account)        # recheck, PnL, pick list, draft descriptions
    submitted, tried = 0, 0
    for p in _candidates(conn, account):
        if submitted >= q0.remaining:
            break
        tried += 1
        aid, cid = p["alpha_id"], p["candidate_id"]
        s = summarize(brain.alpha(aid), brain.check(aid))
        chk = json.dumps(s.get("checks"))
        v, why = verdict(s["sharpe"], chk, require_theme=q.theme_required)
        ch = {c.get("name"): c for c in s.get("checks") or []}
        ppc = (ch.get("POWER_POOL_CORRELATION") or {}).get("result")
        subs = (ch.get("REGULAR_SUBMISSION") or {}).get("value")
        if subs is not None and subs >= DAILY_SUBMISSION_LIMIT:
            _log(conn, account, q.today, "stop", aid, cid, f"daily submission limit reached ({subs})")
            break
        if v != "pp_ok" or ppc != "PASS":
            _log(conn, account, q.today, "skip", aid, cid, f"fresh check: {v} {why} PPC={ppc}")
            continue
        low = quality_gate(s["sharpe"], s["fitness"], s["turnover"], chk, quality)
        if low:
            # the Power Pool rule is only sharpe 1.0; income follows quality, so weak alphas wait
            _log(conn, account, q.today, "skip", aid, cid, "below quality bar: " + "; ".join(low))
            continue
        draft = conn.execute("select text from descriptions where candidate_id=?", (cid,)).fetchone()
        text, issues = verified_description(p["expr"], draft[0] if draft else None, catalog, p["region"] or "USA",
                                            json.loads(p["settings"] or "{}"))
        if text is None:
            _log(conn, account, q.today, "skip", aid, cid, "description not verified: " + "; ".join(issues))
            conn.execute("update picks set note=? where alpha_id=?", ("description not verified", aid))
            conn.commit()
            continue
        conn.execute("insert or replace into descriptions(candidate_id, text, model) values (?,?,?)", (cid, text, "verified"))
        conn.commit()
        if dry_run:
            _log(conn, account, q.today, "dry_run", aid, cid, f"would submit: sharpe {s['sharpe']} TO {s['turnover']}")
            submitted += 1
            continue
        brain.set_description(aid, text)
        res = brain.submit(aid)
        if not res.get("submitted") or "PowerPoolSelected" not in (res.get("tags") or []):
            _log(conn, account, q.today, "submit_failed", aid, cid, res)
            continue
        conn.execute("update picks set status='submitted', decided_at=current_timestamp, note=? where alpha_id=?",
                     (f"auto {res.get('dateSubmitted')}", aid))
        conn.commit()
        _log(conn, account, q.today, "submitted", aid, cid, f"sharpe {s['sharpe']} TO {s['turnover']} at {res.get('dateSubmitted')}")
        submitted += 1
    return {"status": "done", "submitted": submitted, "tried": tried, "quota_left_before": q0.remaining}


def quality_bar(cfg: dict, acfg: dict) -> dict:
    """Defaults, then the config's "quality" block, then per-account overrides.
    An old top-level/per-account "min_sharpe" still works."""
    bar = dict(QUALITY_DEFAULTS, **cfg.get("quality", {}))
    for src in (cfg, acfg):
        if "min_sharpe" in src:
            bar["min_sharpe"] = float(src["min_sharpe"])
    bar.update(acfg.get("quality", {}))
    return bar


def run(db_path: str, catalog_dir: str, accounts: list[str] | None = None, force_dry_run: bool = False) -> dict:
    from .db import connect
    cfg = load_config()
    conn = connect(db_path)
    lanes.migrate(conn)
    conn.executescript(SCHEMA)
    out = {}
    with single_submitter():
        for account, acfg in cfg.get("accounts", {}).items():
            if accounts and account not in accounts:
                continue
            if not acfg.get("enabled"):
                continue
            catalog = Catalog(catalog_dir)
            catalog.add_account_fields(conn, account)
            out[account] = run_account(conn, catalog, account, dry_run=force_dry_run or acfg.get("dry_run", True),
                                       quality=quality_bar(cfg, acfg))
    return out
