"""Tiered funnel for regular and ATOM alphas: fix the check that failed.

Our regular near misses almost all fail LOW_SUB_UNIVERSE_SHARPE: the signal earns
in small, illiquid names (switching to TOP1000 cut Sharpe from ~2.4 to ~1.2).
Settings sweeps can't fix where an alpha makes money, so the funnel changes the
expression for that check and only uses settings where settings are the cause:

  sub-universe sharpe   trade liquid names only, TOP1000
  concentrated weight   rank(), truncation 0.05
  low fitness/turnover  ts_decay_linear smoothing, higher decay
  low sharpe only       other neutralizations
  self-correlation, faded last-2y edge   stop: no change fixes these

Power Pool near misses outside USA (pp_parents): results that pass the Power Pool rules
but sit below the quality bar on Sharpe or fitness. 30 Oct: 22 GLB results passed at
Sharpe 1.1-1.4 with fitness 0.4-0.8, mostly at turnover 0.2-0.4, and none was ever
retried. They get smoothing (ts_decay_linear, decay 10) when turnover is high and
other neutralizations otherwise, within the Power Pool shape (8 operators, 3 fields).
Results above the bar whose only Power Pool problem is LOW_SUB_UNIVERSE_SHARPE (BRAIN
refuses those) get the liquidity fix first: trade only the larger names (rank(cap)).

Each parent gets each fix at most once (tried expression+settings pairs are
skipped) and fixes can stack up to MAX_DEPTH levels.
"""
from __future__ import annotations

import json
import sqlite3

from .expr import analyze
from .pp import MAX_OPS
from .verdict import quality_gate, regular_verdict, verdict

MAX_DEPTH = 2
LIQUID_ONLY = 0.3            # trade only names above this adv20 rank
PP_MIN_SHARPE = 1.1          # Power Pool near misses worth smoothing
PP_SMOOTH_TURNOVER = 0.1     # above this, fitness is lost to trading
PP_GAPS = ("sharpe ", "fitness ", "turnover ")   # quality-bar gaps a setting or smoothing can close
PP_MAX_FIELDS = 3
GROUPS = {"market", "sector", "industry", "subindustry", "country", "exchange"}
SUB_FAIL = "sub-universe sharpe (BRAIN refuses the submission)"


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
        # all funnel runs to 30 Oct: trade-liquid-only fixed the check in 23 of 133 and kept
        # the bar in 19; size-bucket neutralization fixed 3 of 132 (dropped); TOP1000 fixed
        # 103 of 116 but halved Sharpe, so none stayed above the bar
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


def _pp_shape(expr: str) -> bool:
    a = analyze(expr)
    return a.ok and a.n_ops <= MAX_OPS and len(a.names - GROUPS) <= PP_MAX_FIELDS


def pp_fixes(expr: str, settings: dict, turnover: float | None, sub_fail: bool = False) -> list[tuple[str, str, dict]]:
    """Fixes for a Power Pool result below the quality bar on Sharpe/fitness (or failing
    only the sub-universe check: sub_fail), best first."""
    s = dict(settings)
    out: list[tuple[str, str, dict]] = []
    if sub_fail:
        # 30 Oct EUR: trade_when(rank(cap) > 0.3) fixed the check but cost a third of the
        # Sharpe; INDUSTRY neutralization and cap buckets moved it most of the way
        out.append(("pp liquidity: trade larger names only", f"trade_when(rank(cap) > {LIQUID_ONLY}, {expr}, -1)", s))
        if s.get("neutralization") != "INDUSTRY":
            out.append(("pp liquidity: neutralization INDUSTRY", expr, dict(s, neutralization="INDUSTRY")))
        if (s.get("truncation") or 0.08) > 0.05:
            out.append(("pp liquidity: truncation 0.05", expr, dict(s, truncation=0.05)))
        return [o for o in out if _pp_shape(o[1])]
    if (turnover or 0) > PP_SMOOTH_TURNOVER:
        out.append(("pp fitness: ts_decay_linear 5", f"ts_decay_linear({expr}, 5)", s))
        if (s.get("decay") or 0) < 10:
            out.append(("pp fitness: decay 10", expr, dict(s, decay=10)))
    for nn in ("INDUSTRY", "SUBINDUSTRY", "MARKET"):
        if s.get("neutralization") != nn and len(out) < 3:
            out.append((f"pp sharpe: neutralization {nn}", expr, dict(s, neutralization=nn)))
    return [o for o in out if _pp_shape(o[1])]


def pp_parents(conn: sqlite3.Connection, account: str) -> list[sqlite3.Row]:
    """Non-USA results that pass the Power Pool rules but miss the quality bar only on
    Sharpe, fitness or turnover; best Sharpe x fitness first."""
    rows = conn.execute("""select r.*, c.region as cregion, c.id as cid, c.fields from resims r join candidates c
                           on c.id = r.candidate_id where r.account=? and r.status='done' and c.region<>'USA'
                             and r.sharpe >= ? order by r.sharpe * r.fitness desc limit 200""",
                        (account, PP_MIN_SHARPE)).fetchall()
    out = []
    for r in rows:
        v, why = verdict(r["sharpe"], r["checks"])
        gaps = quality_gate(r["sharpe"], r["fitness"], r["turnover"], r["checks"], returns=r["returns"],
                            universe=json.loads(r["settings"] or "{}").get("universe"))
        near_bar = gaps and all(g.startswith(PP_GAPS) and "implausible" not in g for g in gaps)
        if ((v == "pp_ok" and near_bar) or (v == "not_pp" and why == [SUB_FAIL] and not gaps)) \
                and _depth(conn, r["id"]) < MAX_DEPTH:
            out.append(r)
    return out


def parents(conn: sqlite3.Connection, account: str) -> list[sqlite3.Row]:
    """Finished results worth fixing: regular near misses, best Sharpe first."""
    rows = conn.execute("""select r.*, c.region as cregion, c.id as cid, c.fields from resims r join candidates c
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


def _regular_fixes(p) -> list[tuple[str, str, dict]]:
    checks = {c.get("name"): c for c in json.loads(p["checks"] or "[]") or []}
    return fixes(p["expr"], json.loads(p["settings"]), regular_verdict(p["checks"])[1], checks)


def _pp_fixes(p) -> list[tuple[str, str, dict]]:
    return pp_fixes(p["expr"], json.loads(p["settings"]), p["turnover"], sub_fail=verdict(p["sharpe"], p["checks"])[1] == [SUB_FAIL])


def next_jobs(conn: sqlite3.Connection, account: str, room: int, budget=None,
              power_pool: bool = False) -> list[tuple[int, str, str, dict, int]]:
    """Up to `room` (candidate_id, purpose, expr, settings, parent_resim_id) jobs from the best parent
    that still has untried fixes. `budget` (lanes.Budget) caps parents on used-up fields.
    power_pool: fix Power Pool near misses outside USA (pp_parents) instead of regular ones."""
    seen = tried(conn, account)
    total = room
    for p in (pp_parents if power_pool else parents)(conn, account):
        room = min(total, budget.room(p)) if budget is not None else total
        if room <= 0:
            continue
        jobs = []
        for why, expr, st in (_pp_fixes if power_pool else _regular_fixes)(p):
            if (expr.replace(" ", ""), json.dumps(st)) in seen:
                continue
            jobs.append((_candidate_for(conn, p, expr, why), f"funnel: {why}", expr, st, p["id"]))
            if len(jobs) >= room:
                break
        if jobs:
            if budget is not None:
                budget.take(p, len(jobs))
            conn.commit()
            return jobs
    return []
