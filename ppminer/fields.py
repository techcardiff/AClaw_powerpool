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
