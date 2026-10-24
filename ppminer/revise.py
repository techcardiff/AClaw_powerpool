"""Lane A feedback loop: revise explore ideas from their simulation results.

One-shot Qwen ideas on new data mostly land at |sharpe| 0.2-0.5. Instead of
discarding them, an idea with some signal gets up to MAX_DEPTH rounds:
  * sharpe <= -FLIP_AT  -> flip the sign (no LLM)
  * |sharpe| < DROP_BELOW -> drop, there is nothing to work with
  * otherwise Qwen sees the metrics and failing checks and proposes revisions
Every revision is validated locally (<= 8 ops, <= 3 fields, same dataset).
"""
from __future__ import annotations

import json
import sqlite3

from .catalog import Catalog
from .explore import _fields
from .llm import LLM_MODEL, chat_json
from .pp import check

MAX_DEPTH = 3
FLIP_AT = 0.7
DROP_BELOW = 0.3

SYSTEM = """You are a senior WorldQuant BRAIN researcher improving a Power Pool alpha after seeing
its simulation. Keep the economic idea, change ONE or TWO things that address the failures:
- low sharpe: different normalisation (rank / zscore / group_rank by industry or subindustry),
  a slower or faster window, ts_backfill for sparse data, or a related field from the list
- high turnover: slower windows, ts_decay_linear, ts_mean smoothing
- concentrated weight: rank(...) or group_rank(...) instead of raw values
- weak recent years: prefer slower, more fundamental horizons
Rules: <= 8 operators, <= 3 fields, only fields from the list, VECTOR fields need vec_avg/vec_sum,
no scientific notation (write 0.0001, never 1e-4). Positive values = long."""

SCHEMA = {"type": "object", "properties": {"revisions": {"type": "array", "maxItems": 2, "items": {
    "type": "object", "properties": {"expr": {"type": "string"}, "change": {"type": "string"}},
    "required": ["expr", "change"]}}}, "required": ["revisions"]}


def todo(conn: sqlite3.Connection, account: str) -> list[sqlite3.Row]:
    """Simulated explore ideas that missed the bar and have not been revised yet."""
    return conn.execute("""
        select r.id as resim_id, r.sharpe as new_sharpe, r.fitness as new_fitness, r.turnover as new_turnover,
               r.checks, c.* from resims r join candidates c on c.id = r.candidate_id
        where r.account=? and r.status='done' and c.status='explore' and coalesce(c.depth,0) < ?
          and not exists (select 1 from candidates k where k.parent_id = c.id)
          and not exists (select 1 from llm_jobs j where j.candidate_id = c.id and j.kind = 'revise')
        order by abs(r.sharpe) desc""", (account, MAX_DEPTH)).fetchall()


def mark(conn: sqlite3.Connection, candidate_id: int, ok: bool, note: str) -> None:
    """One revise attempt per idea: dropped/failed ideas are not retried every cycle."""
    conn.execute("insert into llm_jobs(candidate_id, kind, model, ok, output) values (?, 'revise', ?, ?, ?)",
                 (candidate_id, LLM_MODEL, int(ok), note[:500]))
    conn.commit()


def _metrics(row) -> str:
    ch = {c.get("name"): c for c in json.loads(row["checks"] or "[]") or []}
    fails = [f"{n} ({c.get('value')} vs limit {c.get('limit')})" for n, c in ch.items() if c.get("result") in ("FAIL", "WARNING")
             and n not in ("MATCHES_THEMES", "OSMOSIS_ALLOCATION", "SELF_CORRELATION", "PROD_CORRELATION", "CLUSTER_TEST")]
    return (f"sharpe {row['new_sharpe']}, fitness {row['new_fitness']}, turnover {row['new_turnover']}; "
            f"failing: {'; '.join(fails) or 'none besides sharpe'}")


def revise(conn: sqlite3.Connection, catalog: Catalog, account: str, row) -> list[dict]:
    sh = row["new_sharpe"] or 0
    if abs(sh) < DROP_BELOW:
        return []
    if sh <= -FLIP_AT:
        return [{"expr": f"-({row['expr']})", "change": f"sign flip (sharpe was {sh})"}]
    ds = json.loads(row["datasets"])[0]
    fields = _fields(conn, account, row["region"], row["delay"], ds)
    lines = "\n".join(f"- {f['field']} [{f['type']}]: {(f['description'] or '')[:120]}" for f in fields)
    prompt = f"""Alpha (dataset {ds}): {row['expr']}
Idea: {(row['reasoning'] or '')[:400]}
Result: {_metrics(row)}
Fields available in {ds}:
{lines}

Propose up to 2 revisions."""
    out = chat_json([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                    SCHEMA, max_tokens=1500, temperature=0.5)
    return out.get("revisions", [])[:2]


def save(conn: sqlite3.Connection, catalog: Catalog, parent, revisions: list[dict]) -> int:
    ds = json.loads(parent["datasets"])
    added = 0
    for v in revisions:
        chk = check(v["expr"], catalog, parent["region"])
        if not chk.shape_ok or chk.datasets != ds or chk.unknown_ops:
            continue
        cur = conn.execute(
            """insert or ignore into candidates(key, expr, region, delay, universe, neutralization, decay, truncation,
                   n_ops, n_fields, fields, categories, datasets, single_category, single_dataset, problems, n_seen,
                   sources, reasoning, family, flags, status, parent_id, depth)
               values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'[]',1,?,?,?,'[]','explore',?,?)""",
            (f"{parent['region']}|{parent['delay']}|{chk.canonical}", v["expr"], parent["region"], parent["delay"],
             parent["universe"], parent["neutralization"], parent["decay"], parent["truncation"], chk.n_ops,
             len(chk.fields), json.dumps(chk.fields), json.dumps(chk.categories), json.dumps(chk.datasets),
             int(chk.single_category), int(chk.single_dataset), json.dumps([f"revise:{parent['id']}:{LLM_MODEL}"]),
             f"{(parent['reasoning'] or '')[:300]} | revision: {v['change']}",
             f"{parent['region']}|{parent['delay']}|" + ",".join(sorted(chk.fields)), parent["id"],
             (parent["depth"] or 0) + 1))
        added += cur.rowcount
    conn.commit()
    return added
