"""Harvest past simulations into the candidates table.

Sources:
  * Alpha Studio  (quant/alpha_results.db, table simulations)
  * Agent memory  (export of alpha_memory*.db attempts + raw_signals, made by
                   scripts/export_agent_memory.py on the 3080)
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from .catalog import Catalog
from .expr import skeleton
from .pp import MIN_SHARPE, check

SUSPICIOUS_SHARPE = 5.0          # single-signal D1 sharpe above this is usually leakage/bug
TURNOVER_MIN, TURNOVER_MAX = 0.01, 0.70

# the old agent's default /simulate settings (quant-agent/orchestrator/config.py)
AGENT_DEFAULTS = dict(universe="TOP3000", neutralization="SUBINDUSTRY", decay=6, truncation=0.08, delay=1)


@dataclass
class Row:
    expr: str
    region: str
    delay: int
    universe: str | None
    neutralization: str | None
    decay: int | None
    truncation: float | None
    sharpe: float
    fitness: float | None
    turnover: float | None
    returns: float | None
    drawdown: float | None
    source: str
    alpha_id: str | None
    reasoning: str | None


def studio_rows(path: str):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    q = """select id, expression, region, delay, universe, neutralization, decay, truncation,
                  sharpe, fitness, turnover, returns, drawdown, alpha_id, account_id
           from simulations where sharpe is not null and expression is not null"""
    for (i, e, reg, d, u, n, dec, tr, sh, fit, to, ret, dd, aid, acct) in c.execute(q):
        yield Row(e, reg or "USA", 1 if d is None else d, u, n, dec, tr, sh, fit, to, ret, dd,
                  f"studio:{i}@{acct or '?'}", aid, None)


def agent_rows(path: str):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    q = """select src, src_id, db_region, account, expression, sharpe, fitness, turnover, returns,
                  drawdown, region, settings, alpha_id, reasoning from rows where expression is not null"""
    for (src, sid, dbreg, acct, e, sh, fit, to, ret, dd, reg, st, aid, why) in c.execute(q):
        s = dict(AGENT_DEFAULTS, region=None)
        try:
            parsed = json.loads(st) if st else {}
            if isinstance(parsed, dict):
                s.update({k: v for k, v in parsed.items() if k in s and v is not None})
        except (ValueError, TypeError):
            pass
        yield Row(e, (reg or s["region"] or dbreg or "USA").upper(), int(s["delay"]), s["universe"], s["neutralization"],
                  s["decay"], s["truncation"], sh, fit, to, ret, dd, f"{src}:{sid}@{acct or '?'}", aid, why)


def _classify(chk, sharpe, turnover):
    flags = []
    if sharpe < 0:
        flags.append("negative")             # needs a sign flip (+1 reverse op)
    if abs(sharpe) >= SUSPICIOUS_SHARPE:
        flags.append("suspicious_sharpe")
    if turnover is not None and not (TURNOVER_MIN <= turnover <= TURNOVER_MAX):
        flags.append("turnover_out")
    if chk.unknown_ops:
        flags.append("unknown_ops")
    if not chk.parsed or chk.unknown:
        return "reject", flags
    if abs(sharpe) < MIN_SHARPE:
        return "weak", flags
    flip_ops = 1 if sharpe < 0 else 0
    fits = chk.shape_ok and chk.n_ops + flip_ops <= 8
    if fits and "turnover_out" not in flags and "suspicious_sharpe" not in flags:
        # mixed categories are fine for Lane A (pure PP needs no theme in weeks 1-6),
        # only theme submissions need a single category
        return ("pp_ready" if chk.single_category else "pp_multi"), flags
    return "purify", flags


def reclassify_multi(conn: sqlite3.Connection) -> int:
    """Apply the pp_multi split to an existing pp.db without re-harvesting."""
    cur = conn.execute("""update candidates set status='pp_multi'
        where status='purify' and problems='[]' and single_category=0 and abs(sharpe) >= 1
          and n_ops + (sharpe < 0) <= 8 and flags not like '%suspicious%' and flags not like '%turnover_out%'""")
    conn.commit()
    rank_families(conn)
    return cur.rowcount


def harvest(conn: sqlite3.Connection, catalog: Catalog, sources) -> dict:
    stats = {"rows": 0, "new": 0, "merged": 0}
    for rows in sources:
        for r in rows:
            stats["rows"] += 1
            chk = check(r.expr, catalog, r.region)
            canon = chk.canonical or r.expr.strip()
            key = f"{r.region}|{r.delay}|{canon}"
            old = conn.execute("select id, sharpe, n_seen, sources from candidates where key=?", (key,)).fetchone()
            if old:
                srcs = json.loads(old["sources"])
                if len(srcs) < 20:
                    srcs.append(r.source)
                better = abs(r.sharpe) > abs(old["sharpe"] or 0)
                conn.execute("update candidates set n_seen=n_seen+1, sources=? where id=?",
                             (json.dumps(srcs), old["id"]))
                if not better:
                    stats["merged"] += 1
                    continue
                conn.execute("delete from candidates where id=?", (old["id"],))
                n_seen, srcs_json = old["n_seen"] + 1, json.dumps(srcs)
            else:
                n_seen, srcs_json = 1, json.dumps([r.source])
                stats["new"] += 1
            status, flags = _classify(chk, r.sharpe, r.turnover)
            conn.execute(
                """insert into candidates(key, expr, region, delay, universe, neutralization, decay, truncation,
                       n_ops, n_fields, fields, categories, datasets, single_category, single_dataset, problems,
                       sharpe, fitness, turnover, returns, drawdown, n_seen, sources, best_alpha_id, reasoning,
                       skeleton, family, flags, status)
                   values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (key, r.expr, r.region, r.delay, r.universe, r.neutralization, r.decay, r.truncation,
                 chk.n_ops, len(chk.fields) + len(chk.unknown), json.dumps(chk.fields), json.dumps(chk.categories),
                 json.dumps(chk.datasets), int(chk.single_category), int(chk.single_dataset),
                 json.dumps(chk.problems), r.sharpe, r.fitness, r.turnover, r.returns, r.drawdown, n_seen,
                 srcs_json, r.alpha_id, r.reasoning, skeleton(r.expr, lambda n: n),
                 f"{r.region}|{r.delay}|" + ",".join(sorted(chk.fields + chk.unknown)),
                 json.dumps(flags), status))
        conn.commit()
    rank_families(conn)
    return stats


def rank_families(conn: sqlite3.Connection) -> None:
    """Within each family (same region/delay/field set) rank by |sharpe| then fitness,
    so selection can take one representative per idea instead of 20 wrappers of it."""
    conn.execute("update candidates set family_rank=null")
    conn.execute("""
        with r as (
          select id, row_number() over (partition by family, status
                   order by abs(sharpe) desc, coalesce(fitness,0) desc) as rk
          from candidates)
        update candidates set family_rank = (select rk from r where r.id = candidates.id)""")
    conn.commit()
