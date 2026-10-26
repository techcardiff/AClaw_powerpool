"""Per-account data access: which datasets/fields this BRAIN account can use.

Consultant accounts see different data than the old competition accounts
(e.g. HT52122 has no model77 but 286 USA datasets). Synced into pp.db so
selection only picks alphas the account can actually simulate.
"""
from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from .brain import BASE, Brain
from .catalog import CATEGORY_ID

SCHEMA = """
create table if not exists account_datasets (
    account text, region text, delay integer, universe text, dataset text, name text, category text,
    field_count integer, user_count integer, alpha_count integer, coverage real, value_score real,
    pyramid_multiplier real, themes text, synced_at timestamp default current_timestamp,
    primary key (account, region, delay, universe, dataset));
create table if not exists account_fields (
    account text, region text, delay integer, universe text, field text, dataset text, category text,
    type text, description text, coverage real, user_count integer, alpha_count integer,
    synced_at timestamp default current_timestamp,
    primary key (account, region, delay, universe, field));
create table if not exists field_blocklist (
    account text, field text, reason text, created_at timestamp default current_timestamp,
    primary key (account, field));
"""


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _pages(brain: Brain, path: str, params: dict, page: int = 50):
    off = 0
    while True:
        j = brain._req("GET", f"{BASE}/{path}", params={**params, "limit": page, "offset": off}).json()
        rows = j.get("results", [])
        yield from rows
        off += page
        if len(rows) < page or off >= j.get("count", 0):
            return


def _cat(d: dict) -> str:
    c = (d.get("category") or {})
    cid = c.get("id") or ""
    return CATEGORY_ID.get((c.get("name") or "").lower(), cid)


def sync(conn: sqlite3.Connection, brain: Brain, account: str, region: str = "USA",
         delay: int = 1, universe: str = "TOP3000", workers: int = 4) -> dict:
    ensure(conn)
    base = dict(instrumentType="EQUITY", region=region, delay=delay, universe=universe)
    datasets = list(_pages(brain, "data-sets", base))
    conn.execute("delete from account_datasets where account=? and region=? and delay=? and universe=?",
                 (account, region, delay, universe))
    for d in datasets:
        conn.execute("insert into account_datasets values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,current_timestamp)",
                     (account, region, delay, universe, d["id"], d.get("name"), _cat(d), d.get("fieldCount"),
                      d.get("userCount"), d.get("alphaCount"), d.get("coverage"), d.get("valueScore"),
                      d.get("pyramidMultiplier"), json.dumps(d.get("themes") or [])))
    conn.commit()

    def fetch(d):
        return d, list(_pages(brain, "data-fields", {**base, "dataset.id": d["id"]}))

    conn.execute("delete from account_fields where account=? and region=? and delay=? and universe=?",
                 (account, region, delay, universe))
    n = 0
    with ThreadPoolExecutor(workers) as ex:
        for d, fields in ex.map(fetch, datasets):
            for f in fields:
                conn.execute("insert or replace into account_fields values (?,?,?,?,?,?,?,?,?,?,?,?,current_timestamp)",
                             (account, region, delay, universe, f["id"], d["id"], _cat(d), f.get("type"),
                              f.get("description"), f.get("coverage"), f.get("userCount"), f.get("alphaCount")))
                n += 1
            conn.commit()
    return {"datasets": len(datasets), "fields": n}


def available(conn: sqlite3.Connection, account: str, region: str, delay: int) -> set[str] | None:
    """Fields this account can use (None if never synced for that region/delay)."""
    ensure(conn)
    rows = conn.execute("select field from account_fields where account=? and region=? and delay=?",
                        (account, region, delay)).fetchall()
    if not rows:
        return None
    blocked = {r[0] for r in conn.execute("select field from field_blocklist where account=?", (account,))}
    return {r[0] for r in rows} - blocked


def block(conn: sqlite3.Connection, account: str, field: str, reason: str) -> None:
    ensure(conn)
    conn.execute("insert or replace into field_blocklist(account, field, reason) values (?,?,?)", (account, field, reason))
    conn.commit()


# ── resumable sync (run by the scheduler a few datasets at a time) ──────────
PROGRESS = """
create table if not exists field_sync_progress (
    account text, region text, delay integer, universe text, dataset text,
    n_fields integer, synced_at timestamp default current_timestamp,
    primary key (account, region, delay, universe, dataset));
"""
REFRESH_DAYS = 14


def sync_step(conn: sqlite3.Connection, brain: Brain, account: str, region: str, delay: int = 1,
              universe: str | None = None, max_datasets: int = 6) -> dict:
    """Sync up to max_datasets more datasets for one account/region and remember progress.
    Never deletes what is already there, so an expired session or a busy database only
    means it continues on the next pass. Returns {'done', 'remaining'}."""
    from . import markets
    ensure(conn)
    conn.executescript(PROGRESS)
    universe = universe or markets.universe(region)
    base = dict(instrumentType="EQUITY", region=region, delay=delay, universe=universe)
    have = conn.execute("""select count(*), max(synced_at) < datetime('now', ?) from account_datasets
                           where account=? and region=? and delay=? and universe=?""",
                        (f"-{REFRESH_DAYS} days", account, region, delay, universe)).fetchone()
    if not have[0] or have[1]:
        for d in _pages(brain, "data-sets", base):
            conn.execute("insert or replace into account_datasets values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,current_timestamp)",
                         (account, region, delay, universe, d["id"], d.get("name"), _cat(d), d.get("fieldCount"),
                          d.get("userCount"), d.get("alphaCount"), d.get("coverage"), d.get("valueScore"),
                          d.get("pyramidMultiplier"), json.dumps(d.get("themes") or [])))
        conn.commit()
    # datasets synced before progress was tracked count as done (no refetch of 76k USA fields)
    conn.execute("""insert or ignore into field_sync_progress(account, region, delay, universe, dataset, n_fields)
                    select account, region, delay, universe, dataset, count(*) from account_fields
                    where account=? and region=? and delay=? and universe=? group by dataset""",
                 (account, region, delay, universe))
    conn.commit()
    todo = [r[0] for r in conn.execute("""
        select d.dataset from account_datasets d where d.account=? and d.region=? and d.delay=? and d.universe=?
          and not exists (select 1 from field_sync_progress p where p.account=d.account and p.region=d.region
                          and p.delay=d.delay and p.universe=d.universe and p.dataset=d.dataset
                          and p.synced_at > datetime('now', ?))
        order by coalesce(d.value_score, 0) desc""", (account, region, delay, universe, f"-{REFRESH_DAYS} days"))]
    done = 0
    for ds in todo[:max_datasets]:
        cat = conn.execute("select category from account_datasets where account=? and region=? and dataset=?",
                           (account, region, ds)).fetchone()[0]
        fields = list(_pages(brain, "data-fields", {**base, "dataset.id": ds}))
        for f in fields:
            conn.execute("insert or replace into account_fields values (?,?,?,?,?,?,?,?,?,?,?,?,current_timestamp)",
                         (account, region, delay, universe, f["id"], ds, cat, f.get("type"),
                          f.get("description"), f.get("coverage"), f.get("userCount"), f.get("alphaCount")))
        conn.execute("insert or replace into field_sync_progress values (?,?,?,?,?,?,current_timestamp)",
                     (account, region, delay, universe, ds, len(fields)))
        conn.commit()
        done += 1
    return {"done": done, "remaining": max(0, len(todo) - done)}
