"""What Qwen needs to know to write an alpha that can actually be submitted.

Qwen's one-shot ideas scored 0% at the submission bar (Oct 27: 0 of ~400 reached
Sharpe 1.5) while harvested multi-dataset alphas reached it half the time. Its prompts
had no bar, no examples of what passes, no evidence about fields and no theme goal.
block() gives all of that in one prompt section:
  * the submission bar and Power Pool limits
  * BRAIN's common failures with the fixes that work (the funnel's, see funnel.fixes)
  * structures of our own alphas that cleared the bar (fields replaced by x, y, z, so
    Qwen copies the shape, not the signal already in our Power Pool)
  * the knowledge graph (kg.block): proven little-used fields, proven pairs, crowded fields
  * the active Power Pool theme's goal when the region is the theme's
"""
from __future__ import annotations

import json
import re
import sqlite3

from . import kg
from .select import _shape, _stems
from .verdict import QUALITY_DEFAULTS, quality_gate

VERSION = "kg1"            # tagged into candidate sources, to compare pass rates per prompt version

RULES = f"""[SUBMISSION BAR - an alpha below it cannot be submitted, so it is wasted]
 Sharpe >= {QUALITY_DEFAULTS['min_sharpe']}, fitness >= {QUALITY_DEFAULTS['min_fitness']}, turnover <= {QUALITY_DEFAULTS['max_turnover']},
 Sharpe of the last 2 years >= {QUALITY_DEFAULTS['min_2y_sharpe']}, sub-universe (liquid names) Sharpe >= {QUALITY_DEFAULTS['min_sub_universe_sharpe']},
 no concentrated weight. Power Pool: <= 8 operators, <= 3 fields (grouping fields like industry are free).
[WHAT USUALLY FAILS, AND THE FIX]
 LOW_SHARPE / LOW_FITNESS: raw levels of one field rarely work. Compare like with like (ratio or
   difference of two related fields), normalise across stocks (rank, group_rank by industry or
   subindustry, group_zscore), and smooth (ts_mean, ts_decay_linear).
 CONCENTRATED_WEIGHT: wrap the final signal in rank(...) or group_rank(..., industry); never output
   a raw ratio that can explode for a few stocks.
 LOW_2Y_SHARPE: the signal faded recently - prefer slower windows (60-252) and fundamental horizons.
 LOW_SUB_UNIVERSE_SHARPE: the signal only works in illiquid names - group_neutralize by size bucket
   (bucket(rank(cap), range="0.1,1,0.1")) or trade_when(rank(adv20) > 0.3, signal, -1).
 HIGH_TURNOVER: ts_decay_linear(signal, 5-10), slower windows, or a higher decay setting.
 Sparse fields (low coverage): ts_backfill(field, 60) first, or nothing trades.
"""


def _placeholders(expr: str, fields: list[str]) -> str:
    out = expr
    for f, tag in zip(sorted(fields, key=len, reverse=True), "xyzuvw"):
        out = re.sub(rf"\b{re.escape(f)}\b", tag, out)
    return out


def examples(conn: sqlite3.Connection, region: str, k: int = 6) -> str:
    """Structures of alphas above the bar, each a different idea: BRAIN-verified results
    first (passing the full quality gate), then harvested history (old simulation metrics).
    An alpha sharing two fields with one already shown is the same idea re-wrapped (funnel
    fixes produce many of those), so it is skipped."""
    bar = (QUALITY_DEFAULTS["min_sharpe"], QUALITY_DEFAULTS["min_fitness"], QUALITY_DEFAULTS["max_turnover"], region)
    verified = conn.execute("""select k.region, k.fields, r.expr, r.sharpe, r.fitness, r.turnover, r.returns, r.checks,
                                      r.settings, null as decay, null as neutralization
                               from resims r join candidates k on k.id = r.candidate_id
                               where r.status='done' and r.sharpe >= ? and r.fitness >= ? and r.turnover <= ?
                               order by (k.region = ?) desc, r.fitness desc limit 300""", bar).fetchall()
    harvested = conn.execute("""select region, fields, expr, sharpe, fitness, turnover, returns, null as checks,
                                       null as settings, decay, neutralization
                                from candidates where status in ('pp_multi', 'pp_ready', 'purify') and family_rank = 1
                                  and sharpe >= ? and fitness >= ? and turnover <= ? and fitness < 4
                                  and n_fields <= 3 and n_ops <= 8
                                order by (region = ?) desc, sharpe desc limit 600""", bar).fetchall()
    lines, shapes, seen = [], set(), []
    for r in [x for x in verified if not quality_gate(x["sharpe"], x["fitness"], x["turnover"], x["checks"],
                                                      returns=x["returns"])][:2] + list(harvested):
        fields = json.loads(r["fields"] or "[]")
        stems = set(_stems(fields).split(","))
        sk = _placeholders(r["expr"], fields)
        shape = _shape(sk)
        if shape in shapes or any(len(stems & s) >= min(2, len(stems)) for s in seen):
            continue
        shapes.add(shape)
        seen.append(stems)
        st = json.loads(r["settings"] or "{}") or {"decay": r["decay"], "neutralization": r["neutralization"]}
        lines.append(f"  {sk}   ({r['region']}, sharpe {r['sharpe']}, fitness {r['fitness']}, "
                     f"decay {st.get('decay')}, {st.get('neutralization')})")
        if len(lines) >= k:
            break
    if not lines:
        return ""
    return ("[STRUCTURES THAT CLEARED THE BAR - our own alphas, fields replaced by x, y, z; reuse the shape\n"
            " with different fields, never these exact signals]\n" + "\n".join(lines) + "\n")


def theme_goal(theme, region: str) -> str:
    if theme is None or (theme.regions and region not in theme.regions):
        return ""
    goal = f"[POWER POOL THEME] '{theme.name}': {theme.raw}."
    if theme.sub_sharpe:
        parts = ", ".join(f"{p} Sharpe > {v}" for p, v in theme.sub_sharpe.items())
        goal += (f" The {parts} part is what decides the match: the signal must work on the "
                 f"Americas stocks of the global universe - USA-proven fields are the natural start.")
    return goal + "\n"


def avoid_stems(conn: sqlite3.Connection) -> set[str]:
    """Field families already in the team's submitted alphas (shared correlation pool)."""
    out = set()
    for (stems,) in conn.execute("select stems from account_submitted"):
        out |= {s for s in (stems or "").split(",") if s}
    return out


def block(conn: sqlite3.Connection, account: str, region: str, dataset: str | None = None, theme=None) -> str:
    return (RULES + examples(conn, region) + kg.block(conn, account, region, dataset, avoid_stems(conn))
            + theme_goal(theme, region))
