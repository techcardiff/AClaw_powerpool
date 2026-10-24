"""Write the mandatory Power Pool description (>= 100 chars, Idea + Rationale)."""
from __future__ import annotations

import json
import sqlite3

from .catalog import Catalog
from .llm import LLM_MODEL, chat_json
from .pp import submit_expr

MIN_LEN = 100

SYSTEM = """You are a senior quant researcher at WorldQuant BRAIN writing the description
for a Power Pool alpha. Be specific and honest: explain the economic intuition,
why each data field captures it, and what each operator does. No hype, no claims
about performance. Plain English, 2-4 sentences per section."""

SCHEMA = {
    "type": "object",
    "properties": {
        "idea": {"type": "string"},
        "rationale_data": {"type": "string"},
        "rationale_ops": {"type": "string"},
    },
    "required": ["idea", "rationale_data", "rationale_ops"],
}


def _field_lines(fields: list[str], catalog: Catalog, region: str) -> str:
    out = []
    for f in fields:
        i = catalog.get(f, region)
        if i:
            out.append(f"- {f} ({i.category}/{i.dataset}, {i.type}): {i.description}")
    return "\n".join(out)


def compose(d: dict) -> str:
    return (f"Idea: {d['idea'].strip()}\n"
            f"Rationale for data used: {d['rationale_data'].strip()}\n"
            f"Rationale for operators used: {d['rationale_ops'].strip()}")


def describe(row: sqlite3.Row, catalog: Catalog) -> dict:
    fields = json.loads(row["fields"])
    prompt = f"""Alpha expression (BRAIN FASTEXPR, region {row['region']}, delay {row['delay']}):
{submit_expr(row['expr'], row['sharpe'])}
(Positive values = long. Explain the direction of THIS expression, including any leading minus sign.)

Data fields:
{_field_lines(fields, catalog, row['region'])}

Settings: neutralization={row['neutralization']}, decay={row['decay']}.
Original research note (may be noisy, use only if consistent with the expression):
{(row['reasoning'] or 'n/a')[:800]}

Return JSON with idea, rationale_data, rationale_ops."""
    d = chat_json([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}], SCHEMA)
    d["text"] = compose(d)
    if len(d["text"]) < MIN_LEN:
        raise ValueError(f"description too short ({len(d['text'])})")
    return d


def save(conn: sqlite3.Connection, cid: int, d: dict) -> None:
    conn.execute("""insert or replace into descriptions(candidate_id, idea, rationale_data, rationale_ops, text, model)
                    values (?,?,?,?,?,?)""",
                 (cid, d["idea"], d["rationale_data"], d["rationale_ops"], d["text"], LLM_MODEL))
    conn.commit()
