"""Rewrite strong multi-category / over-complex signals into Power Pool shape.

35k of the old agent's |sharpe|>=1 alphas fail only because they mix data
categories (e.g. analyst field / cap) or use too many operators. Qwen proposes
single-category, <=8-operator variants that keep the core idea; every proposal
is validated locally with pp.check before it is stored for simulation.
"""
from __future__ import annotations

import json
import sqlite3

from .catalog import Catalog
from .llm import LLM_MODEL, chat_json
from .pp import MAX_FIELDS, MAX_OPS, check

SYSTEM = f"""You are an expert WorldQuant BRAIN alpha researcher. You rewrite alphas so they
satisfy Power Pool rules while keeping the core economic signal:
- at most {MAX_OPS} operators in total, repeats included (+ - * / and comparisons count;
  ts_backfill and group_backfill are free)
- at most {MAX_FIELDS} distinct data fields, ALL from the single target data category
- grouping fields (industry, subindustry, sector, market, country, exchange, currency) are allowed
- use only the operators listed by the user; FASTEXPR syntax
Typical fixes: drop a normaliser from another category (cap, close, volume, returns) and use
rank/zscore/group_rank instead; remove redundant wrappers; replace if_else regime gates
with the plain signal. Prefer simple, readable expressions."""

SCHEMA = {
    "type": "object",
    "properties": {
        "variants": {
            "type": "array", "maxItems": 3,
            "items": {"type": "object",
                      "properties": {"expr": {"type": "string"}, "why": {"type": "string"}},
                      "required": ["expr", "why"]},
        }
    },
    "required": ["variants"],
}


def target_categories(row: sqlite3.Row, catalog: Catalog, wanted: set[str] | None) -> list[str]:
    """Categories the alpha could be purified into: those its fields already use."""
    cats = []
    for f in json.loads(row["fields"]):
        i = catalog.get(f, row["region"])
        if i and i.category not in cats and (not wanted or i.category in wanted):
            cats.append(i.category)
    return cats


def purify(row: sqlite3.Row, target: str, catalog: Catalog) -> list[dict]:
    fields = json.loads(row["fields"])
    lines = []
    for f in fields:
        i = catalog.get(f, row["region"])
        if i:
            lines.append(f"- {f}: category={i.category}, dataset={i.dataset}, type={i.type}. {i.description[:160]}")
    prompt = f"""Original alpha (sharpe {row['sharpe']:.2f}, {row['n_ops']} operators):
{row['expr']}

Fields used:
{chr(10).join(lines)}

Target data category: {target}. Keep only fields of category {target}.
Allowed operators: {', '.join(sorted(catalog.operators))}
VECTOR fields must be wrapped in vec_avg or vec_sum.
Give up to 3 distinct variants."""
    out = chat_json([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                    SCHEMA, max_tokens=2000, temperature=0.5)
    results = []
    for v in out.get("variants", [])[:3]:
        chk = check(v["expr"], catalog, row["region"])
        problems = list(chk.problems)
        if chk.parsed and chk.categories != [target]:
            problems.append(f"categories {chk.categories} != [{target}]")
        if chk.unknown_ops:
            problems.append("unknown ops: " + ",".join(chk.unknown_ops))
        results.append({"expr": v["expr"], "why": v.get("why", ""), "check": chk,
                        "valid": not problems, "problems": problems})
    return results


def save(conn: sqlite3.Connection, row: sqlite3.Row, target: str, results: list[dict]) -> int:
    added = 0
    for r in results:
        chk = r["check"]
        conn.execute("""insert or ignore into rewrites(parent_id, target_category, expr, n_ops, categories, valid,
                            problems, rationale, model) values (?,?,?,?,?,?,?,?,?)""",
                     (row["id"], target, r["expr"], chk.n_ops, json.dumps(chk.categories), int(r["valid"]),
                      json.dumps(r["problems"]), r["why"], LLM_MODEL))
        if not r["valid"]:
            continue
        key = f"{row['region']}|{row['delay']}|{chk.canonical}"
        cur = conn.execute(
            """insert or ignore into candidates(key, expr, region, delay, universe, neutralization, decay, truncation,
                   n_ops, n_fields, fields, categories, datasets, single_category, single_dataset, problems,
                   n_seen, sources, reasoning, family, flags, status)
               values (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,'[]',1,?,?,?,'[]','rewrite')""",
            (key, r["expr"], row["region"], row["delay"], row["universe"], row["neutralization"], row["decay"],
             row["truncation"], chk.n_ops, len(chk.fields), json.dumps(chk.fields), json.dumps(chk.categories),
             json.dumps(chk.datasets), int(chk.single_dataset), json.dumps([f"rewrite:{row['id']}"]),
             f"Rewritten from #{row['id']} ({row['expr']}): {r['why']}",
             f"{row['region']}|{row['delay']}|" + ",".join(sorted(chk.fields))))
        added += cur.rowcount
    conn.commit()
    return added
