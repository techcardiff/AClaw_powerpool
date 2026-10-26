"""Alpha Studio's search templates (Search 1/2/3), for any region an account has synced.

  s1  ts_op(F, w)                      and   group_rank(ts_op(F, w), G)
  s2  same-dataset pairs: (A) / (B)    and   ts_corr(A, B, w)
  s3  trade_when(condition, BASE, -1)  conditions that exist in every region only

F, A, B are the dataset's fields for the account and region, best coverage first;
sparse fields (coverage < 0.9) are wrapped in ts_backfill(F, 60) and VECTOR fields
in vec_avg(F). Pairs stay inside one dataset, so results stay single-dataset (ATOM
and Power Pool themes need that). Every expression passes the local Power Pool
check before it is queued; the grid is capped so a dataset can't swamp the budget.
"""
from __future__ import annotations

import itertools
import json
import sqlite3

from . import markets
from .catalog import Catalog
from .pp import check

TS_OPS = ("ts_rank", "ts_zscore", "ts_delta", "ts_mean")
WINDOWS = (20, 60)
PAIR_WINDOWS = (20, 60)
# Search 3 conditions from Alpha Studio that need only price/volume data (the pcr_oi_*
# option conditions exist in USA only and are left out)
CONDITIONS = (
    "ts_mean(volume, 10) > ts_mean(volume, 60)",
    "ts_corr(close, volume, 20) > 0",
    "ts_corr(close, volume, 20) < 0",
    "ts_zscore(returns, 60) > 2",
    "ts_std_dev(returns, 5) > ts_std_dev(returns, 20)",
    "group_rank(ts_std_dev(returns, 60), sector) > 0.7",
    "ts_arg_max(close, 20) == 0",
)
MAX_PER_RUN = 120


def groups_for(region: str) -> tuple[str, ...]:
    """Multi-country regions: compare within country as well as within industry."""
    return ("industry", "country") if region.upper() in ("EUR", "ASI", "GLB") else ("industry",)


def top_fields(conn: sqlite3.Connection, account: str, region: str, dataset: str, n: int) -> list[sqlite3.Row]:
    return conn.execute("""select field, type, coverage from account_fields
                           where account=? and region=? and dataset=? and type in ('MATRIX', 'VECTOR')
                           order by coalesce(coverage, 0) desc, coalesce(user_count, 0) desc limit ?""",
                        (account, region, dataset, n)).fetchall()


def _wrap(f: sqlite3.Row) -> str:
    x = f"vec_avg({f['field']})" if f["type"] == "VECTOR" else f["field"]
    return f"ts_backfill({x}, 60)" if (f["coverage"] or 0) < 0.9 else x


def generate(conn: sqlite3.Connection, catalog: Catalog, account: str, region: str, kind: str,
             dataset: str | None = None, n_fields: int = 6, base_expr: str | None = None) -> list[dict]:
    """Expressions for one template kind; each dict has expr, kind, check problems (empty = valid)."""
    exprs: list[tuple[str, str]] = []
    if kind in ("s1", "s2"):
        fields = top_fields(conn, account, region, dataset, n_fields)
        wrapped = [_wrap(f) for f in fields]
        if kind == "s1":
            for x, op, w in itertools.product(wrapped, TS_OPS, WINDOWS):
                exprs.append((f"{op}({x}, {w})", "s1 layer 1"))
            for x, op, w, g in itertools.product(wrapped, ("ts_rank", "ts_zscore"), WINDOWS[-1:], groups_for(region)):
                exprs.append((f"group_rank({op}({x}, {w}), {g})", "s1 layer 2"))
        else:
            for a, b in itertools.permutations(wrapped, 2):
                exprs.append((f"({a}) / ({b})", "s2 a/b"))
            for (a, b), w in itertools.product(itertools.combinations(wrapped, 2), PAIR_WINDOWS):
                exprs.append((f"ts_corr({a}, {b}, {w})", "s2 corr"))
    elif kind == "s3":
        if not base_expr:
            raise ValueError("s3 needs a base expression")
        exprs = [(f"trade_when({c}, {base_expr}, -1)", "s3 condition") for c in CONDITIONS]
    else:
        raise ValueError(f"unknown template kind {kind}")
    out = []
    for e, why in exprs[:MAX_PER_RUN]:
        chk = check(e, catalog, region)
        problems = list(chk.problems)
        if kind != "s3" and chk.parsed and chk.datasets and chk.datasets != [dataset]:
            problems.append(f"datasets {chk.datasets} (expected only {dataset})")
        out.append({"expr": e, "kind": why, "ops": chk.n_ops, "fields": chk.fields, "categories": chk.categories,
                    "datasets": chk.datasets, "problems": problems, "check": chk})
    return out


def save(conn: sqlite3.Connection, region: str, rows: list[dict], delay: int = 1, source: str = "template") -> int:
    """Queue valid template expressions as candidates (status 'template'); Lane A picks them up."""
    added = 0
    for r in rows:
        if r["problems"]:
            continue
        chk = r["check"]
        cur = conn.execute(
            """insert or ignore into candidates(key, expr, region, delay, universe, neutralization, decay, truncation,
                   n_ops, n_fields, fields, categories, datasets, single_category, single_dataset, problems, n_seen,
                   sources, reasoning, family, flags, status)
               values (?,?,?,?,?,?,4,0.08,?,?,?,?,?,?,?,'[]',1,?,?,?,'[]','template')""",
            (f"{region}|{delay}|{chk.canonical}", r["expr"], region, delay, markets.universe(region),
             markets.neutralization(region), chk.n_ops, len(chk.fields), json.dumps(chk.fields),
             json.dumps(chk.categories), json.dumps(chk.datasets), int(chk.single_category), int(chk.single_dataset),
             json.dumps([source]), r["kind"], f"{region}|{delay}|" + ",".join(sorted(chk.fields))))
        added += cur.rowcount
    conn.commit()
    return added
