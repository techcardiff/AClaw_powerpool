"""Lane A explore: Qwen proposes simple single-dataset alphas on datasets we
have never mined. Single-dataset Power Pool alphas are also ATOM, which sits
outside the pure Power Pool quota, and fresh data is the best source of low
Power Pool correlation.
"""
from __future__ import annotations

import json
import sqlite3

from .catalog import Catalog
from .llm import LLM_MODEL, chat_json
from . import hints, markets
from .pp import check

SYSTEM = """You are a senior WorldQuant BRAIN researcher creating robust Power Pool alphas that must
clear the submission bar given below. Build mainly on ONE dataset (the one given). Rules:
- use only fields from the lists given, at most 3 fields per alpha; you MAY add ONE proven partner
  field from another dataset (listed under PROVEN PARTNERS) when it makes economic sense
- at most 8 operators in total (+ - * / and comparisons count; ts_backfill is free)
- VECTOR fields must be wrapped in vec_avg(...) or vec_sum(...)
- wrap sparse fields (low coverage) in ts_backfill(field, 60)
- each alpha needs a clear economic hypothesis; positive values = long
- prefer the HIDDEN GEMS: proven by our simulations yet little used on the platform, so the
  alpha is strong AND weakly correlated with what others (and our own Power Pool) hold
- avoid CROWDED fields
- pick decay (0-20; higher = slower, lower turnover) and neutralization (MARKET, SECTOR,
  INDUSTRY or SUBINDUSTRY) for each alpha
- DIVERSITY: every alpha a different structure (level, change, surprise, trend, ratio/spread of
  two related fields, stability, event-gated) and different fields where possible
No scientific notation (write 0.0001, never 1e-4)."""

NEUTRALIZATIONS = ["MARKET", "SECTOR", "INDUSTRY", "SUBINDUSTRY"]

SCHEMA = {
    "type": "object",
    "properties": {"alphas": {"type": "array", "maxItems": 6, "items": {
        "type": "object", "properties": {
            "expr": {"type": "string"}, "idea": {"type": "string"},
            "decay": {"type": "integer", "minimum": 0, "maximum": 20},
            "neutralization": {"type": "string", "enum": NEUTRALIZATIONS}},
        "required": ["expr", "idea", "decay", "neutralization"]}}},
    "required": ["alphas"],
}


def fresh_datasets(conn: sqlite3.Connection, account: str, region: str = "USA", delay: int = 1,
                   categories: set[str] | None = None) -> list[sqlite3.Row]:
    """Datasets the account can use that no candidate (harvested or explored) uses yet.
    Higher BRAIN value score / pyramid multiplier first, less crowded first."""
    used = set()
    for (ds,) in conn.execute("select datasets from candidates where region=? and delay=?", (region, delay)):
        used |= set(json.loads(ds or "[]"))
    rows = conn.execute("""select * from account_datasets where account=? and region=? and delay=?
                           order by coalesce(value_score,0) desc, coalesce(pyramid_multiplier,1) desc,
                                    coalesce(alpha_count,0) asc""", (account, region, delay)).fetchall()
    synced = {r[0] for r in conn.execute("select distinct dataset from account_fields where account=? and region=? and delay=?",
                                         (account, region, delay))}
    # datasets no other synced account can see go first: alphas built on them can
    # only ever run on this account, so accounts never research the same data
    others = {r[0] for r in conn.execute("select dataset from account_datasets where account!=? and region=? and delay=?",
                                         (account, region, delay))}
    out = [r for r in rows if r["dataset"] not in used and r["dataset"] in synced
           and (not categories or r["category"] in categories)]
    return sorted(out, key=lambda r: r["dataset"] in others)    # stable: exclusive first, order kept


def _fields(conn, account, region, delay, dataset, k=40):
    """The dataset's best-covered fields, minus families overused in our submitted alphas."""
    over = hints.overused(conn)
    rows = conn.execute("""select field, type, description, coverage, user_count, alpha_count from account_fields
                           where account=? and region=? and delay=? and dataset=? and type in ('MATRIX','VECTOR')
                           order by coalesce(coverage,0) desc""", (account, region, delay, dataset)).fetchall()
    return [r for r in rows if not hints.is_overused(r["field"], over)][:k]


def explore(conn: sqlite3.Connection, catalog: Catalog, account: str, ds: sqlite3.Row,
            region: str = "USA", delay: int = 1, theme=None) -> list[dict]:
    fields = _fields(conn, account, region, delay, ds["dataset"])
    if not fields:
        return []
    lines = "\n".join(f"- {f['field']} [{f['type']}, coverage {f['coverage'] or 0:.2f}, "
                      f"{f['alpha_count'] or 0} platform alphas]: {(f['description'] or '')[:150]}" for f in fields)
    prompt = f"""Dataset {ds['dataset']} — {ds['name']} (category {ds['category']}, region {region}, delay {delay}).
Fields:
{lines}

{hints.block(conn, account, region, ds['dataset'], theme)}
Propose up to 6 distinct alphas."""
    out = chat_json([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                    SCHEMA, max_tokens=2000, temperature=0.6)
    results = []
    for a in out.get("alphas", [])[:6]:
        chk = check(a["expr"], catalog, region)
        problems = list(chk.problems)
        if chk.parsed and (ds["dataset"] not in chk.datasets or len(chk.datasets) > 2):
            problems.append(f"datasets {chk.datasets}: must use {ds['dataset']} plus at most one partner dataset")
        results.append({"expr": a["expr"], "idea": a.get("idea", ""), "check": chk, "valid": not problems,
                        "problems": problems, "decay": a.get("decay"), "neutralization": a.get("neutralization")})
    return results


def save(conn: sqlite3.Connection, ds: sqlite3.Row, results: list[dict], region: str = "USA", delay: int = 1) -> int:
    added = 0
    for r in results:
        if not r["valid"]:
            continue
        chk = r["check"]
        cur = conn.execute(
            """insert or ignore into candidates(key, expr, region, delay, universe, neutralization, decay, truncation,
                   n_ops, n_fields, fields, categories, datasets, single_category, single_dataset, problems,
                   n_seen, sources, reasoning, family, flags, status)
               values (?,?,?,?,?,?,?,0.08,?,?,?,?,?,?,?,'[]',1,?,?,?,'[]','explore')""",
            (f"{region}|{delay}|{chk.canonical}", r["expr"], region, delay, markets.universe(region),
             r.get("neutralization") if r.get("neutralization") in NEUTRALIZATIONS else markets.neutralization(region),
             min(max(int(r["decay"]), 0), 20) if r.get("decay") is not None else 4,
             chk.n_ops, len(chk.fields), json.dumps(chk.fields), json.dumps(chk.categories), json.dumps(chk.datasets),
             int(chk.single_category), int(chk.single_dataset),
             json.dumps([f"explore:{ds['dataset']}:{LLM_MODEL}:{hints.VERSION}"]), r["idea"],
             f"{region}|{delay}|" + ",".join(sorted(chk.fields))))
        added += cur.rowcount
    conn.commit()
    return added
