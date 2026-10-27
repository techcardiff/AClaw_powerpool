"""Market exploration in free simulation slots: template alphas in other regions.

Each mining cycle may add ONE multi-simulation (up to 10 alphas) of Alpha Studio's
Search 1 templates (templates.generate, kind "s1": ts_rank/ts_zscore/ts_delta/ts_mean
and group_rank layers) on a dataset of another region. It is queued behind the main
batch on the same slots, so it only runs when a slot is free.

Everything lives in its own database, data/markets.db, apart from pp.db:
  mx_jobs     one row per simulated template alpha, with results and BRAIN's checks
  mx_cursor   which (region, dataset) each account has explored; a dataset is explored
              by one account only (the accounts share a team correlation pool)
Regions take turns; within a region, the least-explored, least-crowded datasets first.
Fields are taken in "fresh" order (least used on the platform) for low correlation.
"""
from __future__ import annotations

import json
import os
import sqlite3

from . import markets, templates
from .catalog import Catalog

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "markets.db")
CHUNK = 10                  # one BRAIN multi-simulation
FIELDS_PER_DATASET = 3      # s1 on 3 fields = 24 + group layers; plenty for several cycles

SCHEMA = """
create table if not exists mx_jobs (
    id integer primary key, account text, region text, universe text, dataset text, kind text,
    expr text, settings text, status text, alpha_id text,
    sharpe real, fitness real, turnover real, returns real, drawdown real, checks text, error text,
    created_at timestamp default current_timestamp, done_at timestamp,
    unique(account, region, expr));
create table if not exists mx_cursor (
    region text, dataset text, account text, started_at timestamp default current_timestamp,
    primary key (region, dataset));
create table if not exists mx_state (key text primary key, value text);
"""


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    c = sqlite3.connect(path, timeout=120)
    c.row_factory = sqlite3.Row
    c.execute("pragma journal_mode=wal")
    c.executescript(SCHEMA)
    return c


def settings(region: str) -> dict:
    return {"instrumentType": "EQUITY", "region": region, "universe": markets.universe(region), "delay": 1, "decay": 4,
            "neutralization": markets.neutralization(region), "truncation": 0.08, "pasteurization": "ON",
            "unitHandling": "VERIFY", "nanHandling": "OFF", "language": "FASTEXPR", "visualization": False}


def _next_region(mx: sqlite3.Connection, account: str, regions: list[str]) -> list[str]:
    """Regions in turn order for this account (round-robin across cycles)."""
    key = f"region:{account}"
    row = mx.execute("select value from mx_state where key=?", (key,)).fetchone()
    i = (int(row[0]) + 1) % len(regions) if row else 0
    mx.execute("insert into mx_state values (?, ?) on conflict(key) do update set value=excluded.value", (key, str(i)))
    mx.commit()
    return regions[i:] + regions[:i]


def _dataset(conn: sqlite3.Connection, mx: sqlite3.Connection, account: str, region: str) -> str | None:
    """This account's current dataset in the region, or a new one nobody has explored."""
    open_ds = [r[0] for r in mx.execute("select dataset from mx_cursor where account=? and region=?", (account, region))]
    for ds in open_ds:
        if _pending_templates(conn, mx, account, region, ds):
            return ds
    taken = {r[0] for r in mx.execute("select dataset from mx_cursor where region=?", (region,))}
    for (ds,) in conn.execute("""select d.dataset from account_datasets d where d.account=? and d.region=?
                                   and exists (select 1 from account_fields f where f.account=d.account and f.region=d.region
                                               and f.dataset=d.dataset)
                                 order by coalesce(d.value_score,0) desc, coalesce(d.alpha_count,0) asc""", (account, region)):
        # claim only a dataset that actually yields templates for this account
        if ds not in taken and _pending_templates(conn, mx, account, region, ds):
            # the accounts mine in parallel: the primary key makes the claim atomic, and a
            # dataset the other account claimed first is simply skipped
            claimed = mx.execute("insert or ignore into mx_cursor(region, dataset, account) values (?,?,?)",
                                 (region, ds, account)).rowcount
            mx.commit()
            if claimed:
                return ds
    return None


def _pending_templates(conn, mx, account: str, region: str, dataset: str, catalog: Catalog | None = None) -> list[dict]:
    cat = catalog or _catalog(conn, account)
    # least-used fields first; datasets without platform alphas fall back to best coverage
    rows = templates.generate(conn, cat, account, region, "s1", dataset, FIELDS_PER_DATASET, order="fresh") \
        or templates.generate(conn, cat, account, region, "s1", dataset, FIELDS_PER_DATASET, order="coverage")
    done = {r[0] for r in mx.execute("select expr from mx_jobs where account=? and region=? and dataset=?",
                                     (account, region, dataset))}
    return [r for r in rows if not r["problems"] and r["expr"] not in done]


_CATALOGS: dict[str, Catalog] = {}


def _catalog(conn, account: str) -> Catalog:
    if account not in _CATALOGS:
        c = Catalog(os.path.join(ROOT, "data", "catalog"))
        c.add_account_fields(conn, account)
        _CATALOGS[account] = c
    return _CATALOGS[account]


def next_chunk(conn: sqlite3.Connection, mx: sqlite3.Connection, account: str, regions: list[str]) -> list[dict]:
    """Queue up to CHUNK template alphas for this account in the next region with work left.
    Returns [{"id", "expr", "settings"}] (rows inserted into mx_jobs with status 'queued')."""
    for region in _next_region(mx, account, [r for r in regions if r != "USA"] or regions):
        ds = _dataset(conn, mx, account, region)
        if ds is None:
            continue
        todo = _pending_templates(conn, mx, account, region, ds)[:CHUNK]
        if not todo:
            continue
        st = settings(region)
        out = []
        for t in todo:
            cur = mx.execute("""insert or ignore into mx_jobs(account, region, universe, dataset, kind, expr, settings, status)
                                values (?,?,?,?,?,?,?,'queued')""",
                             (account, region, st["universe"], ds, t["kind"], t["expr"], json.dumps(st)))
            if cur.rowcount:
                out.append({"id": cur.lastrowid, "expr": t["expr"], "settings": st})
        mx.commit()
        if out:
            return out
    return []


def record(mx: sqlite3.Connection, job_id: int, result) -> None:
    """Save one simulation result (summarize() dict) or an Exception."""
    if isinstance(result, Exception):
        mx.execute("update mx_jobs set status='error', error=?, done_at=current_timestamp where id=?",
                   (str(result)[:500], job_id))
    else:
        mx.execute("""update mx_jobs set status='done', alpha_id=?, sharpe=?, fitness=?, turnover=?, returns=?, drawdown=?,
                      checks=?, done_at=current_timestamp where id=?""",
                   (result["alpha_id"], result["sharpe"], result["fitness"], result["turnover"], result["returns"],
                    result["drawdown"], json.dumps(result.get("checks")), job_id))
    mx.commit()


def clear_unfinished(mx: sqlite3.Connection) -> int:
    n = mx.execute("delete from mx_jobs where status in ('queued', 'running')").rowcount
    mx.commit()
    return n
