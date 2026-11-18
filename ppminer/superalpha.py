"""Super alphas: a combination of the account's own submitted alphas, one submission a day.

BRAIN (18 Nov, HT52122 has the SUPER_ALPHA permission, YO50689 not yet):
  * simulation type SUPER with a `selection` expression (which submitted alphas take part) and a
    `combo` expression (how they are weighted); no `regular` code at all
  * selection variables that exist: turnover, self_correlation, prod_correlation, operator_count,
    long_count (sharpe, fitness, region, datacategory do not)
  * submission needs a FULL simulation, a selection and a combo description of >= 100 chars each,
    SELF_CORRELATION and PROD_CORRELATION computed and PASS, and SUPER_SUBMISSION (limit 1 a day)
  * first runs, USA TOP3000, selection limit 20: combo_a(alpha) on all alphas Sharpe 3.9; keeping
    alphas with self_correlation < 0.4 and turnover < 0.3, combo_a over 500 days, Sharpe 5.8
    fitness 7.5. A combination of many alphas earns far above one alpha's numbers, so the single
    alpha caps (fitness <= 6) don't apply here; BRAIN's own checks and correlation decide.

Per account and day (config accounts.<id>.super = {"enabled": true, "dry_run": true}), inside
autosubmit, so it stays the single submitter:
  1. stop if BRAIN's SUPER_SUBMISSION shows today's super alpha already in
  2. if no simulated, unsubmitted variant from the last few days is left, simulate the grid
  3. best fitness first: descriptions, fresh check, every check PASS (warnings only on
     competition/osmosis/theme), correlation PASS; submit one
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

from .brain import BASE, AuthRequired, Brain

SCHEMA = """
create table if not exists super_alphas (
    account text, alpha_id text, selection text, combo text, settings text,
    sharpe real, fitness real, turnover real, checks text, status text default 'simulated',
    detail text, created_at timestamp default current_timestamp, updated_at timestamp,
    primary key (account, alpha_id));
"""
SELECTIONS = ("and(self_correlation < 0.4, turnover < 0.3)",
              "self_correlation < 0.5",
              "and(self_correlation < 0.5, prod_correlation < 0.5)")
COMBOS = ('combo_a(alpha, nlength=500)', 'combo_a(alpha, mode="algo2")')
SELECTION_LIMIT = 20
FRESH_DAYS = 5                       # simulated variants older than this are simulated again
OK_WARNINGS = {"MATCHES_COMPETITION", "OSMOSIS_ALLOCATION", "MATCHES_THEMES", "MATCHES_PYRAMID"}
MUST_PASS = ("SELF_CORRELATION", "PROD_CORRELATION")
MIN_DESCRIPTION = 100


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def settings(region: str = "USA", universe: str = "TOP3000", delay: int = 1, limit: int = SELECTION_LIMIT) -> dict:
    return {"instrumentType": "EQUITY", "region": region, "universe": universe, "delay": delay, "decay": 0,
            "neutralization": "SUBINDUSTRY", "truncation": 0.08, "pasteurization": "ON", "unitHandling": "VERIFY",
            "nanHandling": "OFF", "selectionHandling": "POSITIVE", "selectionLimit": limit, "language": "FASTEXPR",
            "visualization": False, "simulationMode": "FULL"}


# ── descriptions (>= 100 chars each, written from the expressions, never guessed) ──────────
_COND = {
    "self_correlation": "correlation with my other submitted alphas",
    "prod_correlation": "correlation with the alphas already in BRAIN's production pool",
    "turnover": "daily turnover",
    "operator_count": "number of operators",
    "long_count": "number of long positions",
}
_CMP = {"<": "below", "<=": "at most", ">": "above", ">=": "at least"}


def _conditions(selection: str) -> list[str]:
    out = []
    for var, op, val in re.findall(r"(\w+)\s*(<=|>=|<|>)\s*([\d.]+)", selection):
        if var in _COND:
            out.append(f"{_COND[var]} {_CMP[op]} {val}")
    return out


def describe(selection: str, combo: str, st: dict) -> tuple[str, str]:
    conds = _conditions(selection)
    who = (f"my own submitted {st.get('region', 'USA')} delay-{st.get('delay', 1)} alphas "
           f"({st.get('universe', 'TOP3000')})")
    if conds:
        sel = (f"Selection: from {who}, keep only those with {' and '.join(conds)}, up to "
               f"{st.get('selectionLimit', SELECTION_LIMIT)} of them. ")
    else:
        sel = f"Selection: all of {who}, up to {st.get('selectionLimit', SELECTION_LIMIT)} of them. "
    sel += ("Low mutual correlation means each component carries a different signal, so the combination "
            "diversifies risk instead of repeating one idea; capping turnover keeps trading costs down.")
    n = re.search(r"nlength\s*=\s*(\d+)", combo)
    m = re.search(r"mode\s*=\s*[\"'](\w+)[\"']", combo)
    days = n.group(1) if n else "250"
    cmb = (f"Combination: combo_a weights each selected alpha by balancing its historical return against "
           f"its variability over the most recent {days} trading days"
           + (f" (weighting scheme {m.group(1)})" if m else "")
           + ". Components that earned steadily get more weight and noisy ones less, and the weighted sum "
             "of their positions is the super alpha.")
    return sel, cmb


# ── simulation ────────────────────────────────────────────────────────────────
def simulate(brain: Brain, selection: str, combo: str, st: dict, max_wait: int = 1800) -> dict:
    r = brain._req("POST", f"{BASE}/simulations", json={"type": "SUPER", "settings": st,
                                                        "selection": selection, "combo": combo})
    sim = brain._poll(r.headers["Location"], max_wait=max_wait)
    if "alpha" not in sim:
        raise RuntimeError(f"super simulation failed: {sim.get('status')} {sim.get('message', '')}")
    return brain.alpha(sim["alpha"])


def simulate_grid(conn, brain: Brain, account: str, st: dict | None = None, selections=SELECTIONS,
                  combos=COMBOS, workers: int = 3, log=print) -> int:
    migrate(conn)
    st = st or settings()
    jobs = [(s, c) for s in selections for c in combos]

    def one(job):
        try:
            return job, simulate(brain, job[0], job[1], st)
        except AuthRequired:
            raise
        except Exception as e:
            return job, e

    n = 0
    with ThreadPoolExecutor(workers) as ex:
        for (sel, combo), a in ex.map(one, jobs):
            if isinstance(a, Exception):
                log(f"  super {sel} | {combo}: {a}")
                continue
            i = a.get("is") or {}
            conn.execute("""insert or replace into super_alphas(account, alpha_id, selection, combo, settings, sharpe,
                                fitness, turnover, checks, status, updated_at)
                            values (?,?,?,?,?,?,?,?,?,'simulated',current_timestamp)""",
                         (account, a["id"], sel, combo, json.dumps(st), i.get("sharpe"), i.get("fitness"),
                          i.get("turnover"), json.dumps(i.get("checks") or [])))
            conn.commit()
            log(f"  super {a['id']} sharpe {i.get('sharpe')} fitness {i.get('fitness')} TO {i.get('turnover')} "
                f"| {sel} | {combo}")
            n += 1
    return n


def judge(checks: list[dict]) -> tuple[str, str]:
    """('ok' | 'wait' | 'fail', reason) for a super submission from BRAIN's /check items."""
    ch = {c.get("name"): c for c in checks or []}
    sub = ch.get("SUPER_SUBMISSION") or {}
    if sub.get("value") is not None and sub.get("limit") is not None and sub["value"] >= sub["limit"]:
        return "done_today", f"SUPER_SUBMISSION {sub['value']}/{sub['limit']}"
    for n in MUST_PASS:
        res = (ch.get(n) or {}).get("result")
        if res in (None, "PENDING"):
            return "wait", f"{n} {res or 'missing'}"
    bad = sorted(n for n, c in ch.items() if c.get("result") == "FAIL"
                 or (c.get("result") in ("WARNING", "PENDING") and n not in OK_WARNINGS))
    if bad:
        return "fail", "not passing: " + ", ".join(f"{n}={ch[n].get('result')}:{ch[n].get('value')}" for n in bad)
    return "ok", ""


def run_account(conn, account: str, dry_run: bool, brain: Brain | None = None, log=None,
                st: dict | None = None) -> dict:
    from . import cookies as ck
    migrate(conn)
    log = log or (lambda *a, **k: None)
    if brain is None:
        brain = Brain.from_cookie(ck.load_cookie(account)) if account in ck.saved_accounts() else None
    if brain is None or not brain.is_authenticated():
        return {"status": "waiting_login"}
    fresh = lambda: conn.execute(
        f"""select * from super_alphas where account=? and status='simulated'
            and created_at >= datetime('now', '-{FRESH_DAYS} days') order by fitness desc""", (account,)).fetchall()
    rows = fresh()
    simulated = 0
    if not rows:
        simulated = simulate_grid(conn, brain, account, st, log=lambda m: log("super_sim", detail=m.strip()))
        rows = fresh()
    for row in rows:
        aid = row["alpha_id"]
        sel_d, combo_d = describe(row["selection"], row["combo"], json.loads(row["settings"] or "{}"))
        assert len(sel_d) >= MIN_DESCRIPTION and len(combo_d) >= MIN_DESCRIPTION
        if not dry_run:
            brain._req("PATCH", f"{BASE}/alphas/{aid}", json={
                "selection": {"code": row["selection"], "description": sel_d},
                "combo": {"code": row["combo"], "description": combo_d}})
        try:
            c = brain.check(aid, max_wait=300)
        except TimeoutError:
            log("super_skip", aid, "BRAIN check still running; next run")
            continue
        checks = (c.get("is") or {}).get("checks") or []
        if dry_run:   # without descriptions BRAIN fails the two length checks; judge as if they were set
            checks = [x for x in checks if not x.get("name", "").endswith("DESCRIPTION_LENGTH")]
        verdict, why = judge(checks)
        conn.execute("update super_alphas set checks=?, detail=?, updated_at=current_timestamp where account=? and alpha_id=?",
                     (json.dumps(checks), why or "ok", account, aid))
        conn.commit()
        if verdict == "done_today":
            log("super_quota", aid, why)
            return {"status": "quota_full", "submitted": 0, "simulated": simulated}
        if verdict == "fail":
            conn.execute("update super_alphas set status='failed' where account=? and alpha_id=?", (account, aid))
            conn.commit()
            log("super_skip", aid, why)
            continue
        if verdict == "wait":
            log("super_skip", aid, why + "; next run")
            continue
        if dry_run:
            log("super_dry_run", aid, f"would submit super alpha: sharpe {row['sharpe']} fitness {row['fitness']} "
                f"| {row['selection']} | {row['combo']}")
            return {"status": "done", "submitted": 0, "would_submit": aid, "simulated": simulated}
        res = brain.submit(aid)
        if not res.get("submitted"):
            log("super_submit_failed", aid, res)
            conn.execute("update super_alphas set detail=? where account=? and alpha_id=?",
                         (f"refused: {res.get('refused')}", account, aid))
            conn.commit()
            continue
        conn.execute("update super_alphas set status='submitted', detail=?, updated_at=current_timestamp "
                     "where account=? and alpha_id=?", (f"{res.get('dateSubmitted')}", account, aid))
        conn.commit()
        log("super_submitted", aid, f"sharpe {row['sharpe']} fitness {row['fitness']} | {row['selection']} | {row['combo']}")
        return {"status": "done", "submitted": 1, "simulated": simulated}
    return {"status": "done", "submitted": 0, "simulated": simulated}
