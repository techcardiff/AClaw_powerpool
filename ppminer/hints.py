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
import time
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


# Fields the team's submitted alphas already use (shared correlation pool) are judged by
# evidence, not by count: BRAIN's Power Pool correlation results on our alphas using them.
#   hidden    >= MIN_TESTS correlation results and < MIN_PASS_RATE of them pass, or, with
#             fewer results, used in >= HIDE_UNTESTED submitted alphas: Qwen never sees it
#   careful   any other field used in >= OVERUSED submitted alphas: shown with its count and
#             pass rate, to be combined with a new partner field and a different structure
# cap and adv20 never count: the size-bucket and liquid-names fixes use them for grouping.
OVERUSED = 3
MIN_TESTS = 10
MIN_PASS_RATE = 0.25
HIDE_UNTESTED = 20
FIX_FIELDS = {"cap", "adv20"}
CACHE_SECONDS = 3600
_CACHE: dict = {"at": 0.0, "tiers": None}


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


def _used(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """(name, uses) of fields in >= OVERUSED submitted alphas, most used first: a family 'x_y*'
    when its name is specific (has an underscore: horizon variants of one signal), else the
    exact field - a family like 'fnd6' would also cover unrelated fnd6_* fields."""
    fams = [(f"{s}*", n) for s, n in submitted_usage(conn).items() if n >= OVERUSED and "_" in s and s not in FIX_FIELDS]
    fields = [(f, n) for f, n in submitted_fields(conn).items() if n >= OVERUSED and f not in FIX_FIELDS
              and not any(f.startswith(x[:-1] + "_") for x, _ in fams)]
    return sorted(fams + fields, key=lambda x: -x[1])


def is_overused(field: str, avoid: list) -> bool:
    """`avoid`: (name, ...) tuples, name an exact field or a family 'x*'."""
    return any(field == x[0] or (x[0].endswith("*") and field.startswith(x[0][:-1] + "_")) for x in avoid)


def correlation_evidence(conn: sqlite3.Connection, names: list[str]) -> dict[str, tuple[int, int]]:
    """name -> (Power Pool correlation results, passes) over our alphas using that field."""
    out = {n: [0, 0] for n in names}
    for fields, checks in conn.execute("""select k.fields, r.checks from resims r join candidates k on k.id = r.candidate_id
                                          where r.status='done' and r.checks like '%POWER_POOL_CORRELATION%'"""):
        ppc = next((c for c in json.loads(checks or "[]") or [] if c.get("name") == "POWER_POOL_CORRELATION"), {})
        if ppc.get("result") not in ("PASS", "FAIL", "WARNING"):
            continue
        fs = json.loads(fields or "[]")
        for n in names:
            if any(is_overused(f, [(n,)]) for f in fs):
                out[n][0] += 1
                out[n][1] += ppc["result"] == "PASS"
    return {n: (t, p) for n, (t, p) in out.items()}


def tiers(conn: sqlite3.Connection) -> dict[str, list[tuple[str, int, int, int]]]:
    """{"hidden": [...], "careful": [...]} of (name, submitted uses, correlation results, passes)."""
    if _CACHE["tiers"] is not None and time.time() - _CACHE["at"] < CACHE_SECONDS:
        return _CACHE["tiers"]
    used = _used(conn)
    ev = correlation_evidence(conn, [n for n, _ in used])
    out = {"hidden": [], "careful": []}
    for n, uses in used:
        t, p = ev[n]
        saturated = (t >= MIN_TESTS and p / t < MIN_PASS_RATE) or (t < MIN_TESTS and uses >= HIDE_UNTESTED)
        out["hidden" if saturated else "careful"].append((n, uses, t, p))
    _CACHE.update(at=time.time(), tiers=out)
    return out


def overused(conn: sqlite3.Connection) -> list[tuple[str, int, int, int]]:
    """Fields Qwen never sees (field lists, proven pairs)."""
    return tiers(conn)["hidden"]


def _ev(uses: int, t: int, p: int) -> str:
    return f"in {uses} submitted, " + (f"{round(100 * p / t)}% of {t} new alphas passed PP correlation" if t
                                        else "no correlation results yet")


def overused_note(conn: sqlite3.Connection, k: int = 18) -> str:
    tr = tiers(conn)
    out = ""
    if tr["hidden"]:
        out += ("[USED UP IN OUR SUBMITTED ALPHAS - new alphas on them keep failing Power Pool correlation;\n"
                " never use these (x* = every horizon variant x_...)]\n"
                + "\n".join(f"  {n} ({_ev(u, t, p)})" for n, u, t, p in tr["hidden"][:k]) + "\n")
    if tr["careful"]:
        out += ("[IMPORTANT BUT ALREADY IN OUR SUBMITTED ALPHAS - usable: pair it with a NEW partner field\n"
                " (not one it is already combined with) and a different structure, never a re-wrap of a known alpha]\n"
                + "\n".join(f"  {n} ({_ev(u, t, p)})" for n, u, t, p in tr["careful"][:k]) + "\n")
    return out


def block(conn: sqlite3.Connection, account: str, region: str, dataset: str | None = None, theme=None) -> str:
    # hidden gems leave out every family we submitted at all; field lists and pairs leave out the used-up ones
    return (RULES + examples(conn, region)
            + kg.block(conn, account, region, dataset, set(submitted_usage(conn)), overused(conn))
            + overused_note(conn) + theme_goal(theme, region))
