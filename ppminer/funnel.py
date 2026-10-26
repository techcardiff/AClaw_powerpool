"""Tiered funnel for regular and ATOM alphas: fix the check that failed.

Our regular near misses almost all fail LOW_SUB_UNIVERSE_SHARPE: the signal earns
in small, illiquid names (switching to TOP1000 cut Sharpe from ~2.4 to ~1.2).
Settings sweeps can't fix where an alpha makes money, so the funnel changes the
expression for that check and only uses settings where settings are the cause:

  sub-universe sharpe   size-bucket neutralization, trade liquid names only, TOP1000
  concentrated weight   rank(), truncation 0.05
  low fitness/turnover  ts_decay_linear smoothing, higher decay
  low sharpe only       other neutralizations
  self-correlation, faded last-2y edge   stop: no change fixes these

Each parent gets each fix at most once (tried expression+settings pairs are
skipped) and fixes can stack up to MAX_DEPTH levels.
"""
from __future__ import annotations

import json
import sqlite3

from .expr import analyze
from .verdict import regular_verdict

MAX_DEPTH = 2
LIQUID_ONLY = 0.3            # trade only names above this adv20 rank


def _depth(conn: sqlite3.Connection, resim_id: int) -> int:
    d, rid = 0, resim_id
    while rid is not None and d < 10:
        row = conn.execute("select parent_resim_id, purpose from resims where id=?", (rid,)).fetchone()
        if row is None or not (row[1] or "").startswith(("funnel", "tune")):
            break
        d, rid = d + 1, row[0]
    return d


def fixes(expr: str, settings: dict, fails: list[str], checks: dict) -> list[tuple[str, str, dict]]:
    """Candidate (why, expression, settings) fixes for one result, best first."""
    if "SELF_CORRELATION" in fails:
        return []
    two_y = (checks.get("LOW_2Y_SHARPE") or {})
    if two_y.get("result") == "FAIL" and (two_y.get("value") or 0) < 1.0:
        return []
    out: list[tuple[str, str, dict]] = []
    s = dict(settings)
    if "LOW_SUB_UNIVERSE_SHARPE" in fails:
        out.append(("liquidity: neutralize within size buckets",
                    f'group_neutralize({expr}, bucket(rank(cap), range="0.1,1,0.1"))', s))
        out.append(("liquidity: trade liquid names only",
                    f"trade_when(rank(adv20) > {LIQUID_ONLY}, {expr}, -1)", s))
        if s.get("universe") == "TOP3000":
            out.append(("liquidity: TOP1000 universe", expr, dict(s, universe="TOP1000")))
    if "CONCENTRATED_WEIGHT" in fails:
        out.append(("concentration: rank()", f"rank({expr})", s))
        if (s.get("truncation") or 0.08) > 0.05:
            out.append(("concentration: truncation 0.05", expr, dict(s, truncation=0.05)))
    if "LOW_FITNESS" in fails or "HIGH_TURNOVER" in fails:
        out.append(("turnover: ts_decay_linear 5", f"ts_decay_linear({expr}, 5)", s))
        if (s.get("decay") or 0) < 10:
            out.append(("turnover: decay 10", expr, dict(s, decay=10)))
    if fails and set(fails) <= {"LOW_SHARPE"}:
        for nn in ("INDUSTRY", "SECTOR"):
            if s.get("neutralization") != nn:
                out.append((f"sharpe: neutralization {nn}", expr, dict(s, neutralization=nn)))
    return [o for o in out if analyze(o[1]).ok]


def parents(conn: sqlite3.Connection, account: str) -> list[sqlite3.Row]:
    """Finished results worth fixing: regular near misses, best Sharpe first."""
    rows = conn.execute("""select r.*, c.region as cregion, c.id as cid from resims r join candidates c
                           on c.id = r.candidate_id where r.account=? and r.status='done' and r.sharpe >= 1.2
                           order by r.sharpe desc limit 400""", (account,)).fetchall()
    return [r for r in rows if regular_verdict(r["checks"])[0] == "near" and _depth(conn, r["id"]) < MAX_DEPTH]


def tried(conn: sqlite3.Connection, account: str) -> set[tuple[str, str]]:
    return {(e.replace(" ", ""), st) for e, st in conn.execute("select expr, settings from resims where account=?", (account,))}


def _candidate_for(conn: sqlite3.Connection, parent: sqlite3.Row, expr: str, why: str) -> int:
    """A changed expression needs its own candidate row (region/delay/universe come from the parent)."""
    if expr == parent["expr"]:
        return parent["candidate_id"]
    p = conn.execute("select * from candidates where id=?", (parent["candidate_id"],)).fetchone()
    s = analyze(expr)
    key = f"{p['region']}|{p['delay']}|{s.canonical}"
    conn.execute("""insert or ignore into candidates(key, expr, region, delay, universe, neutralization, decay, truncation,
                        n_ops, fields, categories, datasets, single_category, single_dataset, problems, n_seen, sources,
                        reasoning, family, flags, status, parent_id, depth)
                    values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'[]',1,?,?,?,'[]','funnel',?,?)""",
                 (key, expr, p["region"], p["delay"], p["universe"], p["neutralization"], p["decay"], p["truncation"],
                  s.n_ops, p["fields"], p["categories"], p["datasets"], p["single_category"], p["single_dataset"],
                  json.dumps([f"funnel:{parent['alpha_id']}"]), f"{why} (from {parent['alpha_id']})",
                  p["family"], p["id"], (p["depth"] or 0) + 1))
    return conn.execute("select id from candidates where key=?", (key,)).fetchone()[0]


def next_jobs(conn: sqlite3.Connection, account: str, room: int) -> list[tuple[int, str, str, dict, int]]:
    """Up to `room` (candidate_id, purpose, expr, settings, parent_resim_id) jobs from the best parent
    that still has untried fixes."""
    seen = tried(conn, account)
    for p in parents(conn, account):
        checks = {c.get("name"): c for c in json.loads(p["checks"] or "[]") or []}
        _, fails = regular_verdict(p["checks"])
        settings = json.loads(p["settings"])
        jobs = []
        for why, expr, st in fixes(p["expr"], settings, fails, checks):
            if (expr.replace(" ", ""), json.dumps(st)) in seen:
                continue
            jobs.append((_candidate_for(conn, p, expr, why), f"funnel: {why}", expr, st, p["id"]))
            if len(jobs) >= room:
                break
        if jobs:
            conn.commit()
            return jobs
    return []
