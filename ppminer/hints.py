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
from collections import Counter

from . import kg
from .expr import GROUPING_FIELDS, analyze
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


# A field (or specific field family) in this many of the team's submitted alphas is overused:
# Qwen never sees it (field lists, pairs) and is told to avoid it. cap and adv20 stay: the size-bucket and
# liquid-names fixes above use them for grouping, not as signal.
OVERUSED = 3
FIX_FIELDS = {"cap", "adv20"}


def submitted_usage(conn: sqlite3.Connection) -> Counter:
    """Field family -> number of the team's submitted alphas using it (shared correlation pool)."""
    out = Counter()
    for (stems,) in conn.execute("select stems from account_submitted"):
        out.update({s for s in (stems or "").split(",") if s})
    return out


def submitted_fields(conn: sqlite3.Connection) -> Counter:
    """Exact field -> number of the team's submitted alphas using it."""
    out = Counter()
    for (expr,) in conn.execute("select expr from account_submitted"):
        out.update(analyze(expr or "").names - GROUPING_FIELDS)
    return out


def _avoid(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """(name, uses) to avoid, most used first: a family 'x_y*' when its name is specific (has
    an underscore: horizon variants of one signal), else the exact field - a family like
    'fnd6' would also hide unrelated fnd6_* fields, including proven ones."""
    fams = [(f"{s}*", n) for s, n in submitted_usage(conn).items() if n >= OVERUSED and "_" in s and s not in FIX_FIELDS]
    fields = [(f, n) for f, n in submitted_fields(conn).items() if n >= OVERUSED and f not in FIX_FIELDS
              and not any(f.startswith(x[:-1] + "_") for x, _ in fams)]
    return sorted(fams + fields, key=lambda x: -x[1])


def is_overused(field: str, avoid: list[tuple[str, int]]) -> bool:
    return any(field == x or (x.endswith("*") and field.startswith(x[:-1] + "_")) for x, _ in avoid)


def overused(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    return _avoid(conn)


def overused_note(conn: sqlite3.Connection, k: int = 18) -> str:
    top = _avoid(conn)[:k]
    if not top:
        return ""
    return ("[OVERUSED IN OUR SUBMITTED ALPHAS - fails Power Pool correlation; never use these fields\n"
            " (x* = every horizon variant x_...); number = submitted alphas using it]\n  "
            + ", ".join(f"{s} ({n})" for s, n in top) + "\n")


def block(conn: sqlite3.Connection, account: str, region: str, dataset: str | None = None, theme=None) -> str:
    # hidden gems leave out every family we submitted at all; field lists and pairs leave out the overused ones
    return (RULES + examples(conn, region)
            + kg.block(conn, account, region, dataset, set(submitted_usage(conn)), overused(conn))
            + overused_note(conn) + theme_goal(theme, region))
