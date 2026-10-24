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
from . import markets
from .pp import check

SYSTEM = """You are a senior WorldQuant BRAIN researcher creating simple, robust Power Pool alphas
from ONE dataset. Rules:
- use only fields from the list given, at most 2 fields per alpha
- at most 5 operators in total (+ - * / and comparisons count; ts_backfill is free)
- VECTOR fields must be wrapped in vec_avg(...) or vec_sum(...)
- wrap sparse fields (low coverage) in ts_backfill(field, 60)
- each alpha needs a clear economic hypothesis; positive values = long
- slow windows (20-252 days) keep turnover low
- DIVERSITY: every alpha must use a different structure from this menu, and different fields
  where possible:
    level         rank(x) | group_rank(x, industry)
    change        ts_delta(x, d) | x - ts_mean(x, d)
    surprise      ts_zscore(x, d) | ts_rank(x, d)
    trend         ts_regression(x, ts_step(1), d, rettype=2) | ts_decay_linear(x, d)
    spread/ratio  x / y | x - y   (two related fields)
    stability     -ts_std_dev(x, d) / ts_mean(abs(x), d)
    event-gated   trade_when(ts_delta(x, 1) != 0, rank(x), -1)
  Do not wrap every alpha in the same outer operator."""

SCHEMA = {
    "type": "object",
    "properties": {"alphas": {"type": "array", "maxItems": 6, "items": {
        "type": "object", "properties": {"expr": {"type": "string"}, "idea": {"type": "string"}},
        "required": ["expr", "idea"]}}},
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
    rows = conn.execute("""select field, type, description, coverage, user_count, alpha_count from account_fields
                           where account=? and region=? and delay=? and dataset=? and type in ('MATRIX','VECTOR')
                           order by coalesce(coverage,0) desc limit ?""", (account, region, delay, dataset, k)).fetchall()
    return rows


def explore(conn: sqlite3.Connection, catalog: Catalog, account: str, ds: sqlite3.Row,
            region: str = "USA", delay: int = 1) -> list[dict]:
    fields = _fields(conn, account, region, delay, ds["dataset"])
    if not fields:
        return []
    lines = "\n".join(f"- {f['field']} [{f['type']}, coverage {f['coverage'] or 0:.2f}]: {(f['description'] or '')[:150]}"
                      for f in fields)
    prompt = f"""Dataset {ds['dataset']} — {ds['name']} (category {ds['category']}, region {region}, delay {delay}).
Fields:
{lines}

Propose up to 6 distinct alphas."""
    out = chat_json([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                    SCHEMA, max_tokens=2000, temperature=0.6)
    results = []
    for a in out.get("alphas", [])[:6]:
        chk = check(a["expr"], catalog, region)
        problems = list(chk.problems)
        if chk.parsed and chk.datasets != [ds["dataset"]]:
            problems.append(f"datasets {chk.datasets} != [{ds['dataset']}]")
        results.append({"expr": a["expr"], "idea": a.get("idea", ""), "check": chk, "valid": not problems,
                        "problems": problems})
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
               values (?,?,?,?,?,?,4,0.08,?,?,?,?,?,1,1,'[]',1,?,?,?,'[]','explore')""",
            (f"{region}|{delay}|{chk.canonical}", r["expr"], region, delay, markets.universe(region), markets.neutralization(region), chk.n_ops, len(chk.fields),
             json.dumps(chk.fields), json.dumps(chk.categories), json.dumps(chk.datasets),
             json.dumps([f"explore:{ds['dataset']}:{LLM_MODEL}"]), r["idea"],
             f"{region}|{delay}|" + ",".join(sorted(chk.fields))))
        added += cur.rowcount
    conn.commit()
    return added
