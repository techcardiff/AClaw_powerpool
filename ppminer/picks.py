"""Correlation-aware pick list.

1. Take re-simulated alphas that meet the Power Pool bar (verdict pp_ok).
2. Pull each alpha's daily PnL from BRAIN (cached in pnl table).
3. Greedy clustering on daily-PnL correlation over the last 4 years: walk alphas
   best-first; an alpha joins the cluster of the first pick it correlates with
   above the threshold, otherwise it becomes a new pick.
   Already approved/submitted picks are seeded first so decisions stay stable.
4. One pick per cluster, waiting for approval in the dashboard.
"""
from __future__ import annotations

import json
import math
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from .brain import BASE, Brain
from .verdict import verdict

SCHEMA = """
create table if not exists pnl (
    alpha_id text primary key, account text, dates text, cum text,
    fetched_at timestamp default current_timestamp);
create table if not exists picks (
    alpha_id     text primary key,
    resim_id     integer, candidate_id integer, account text,
    cluster      integer,            -- = id of the representative's resim
    score        real,
    max_corr     real,               -- highest |corr| with another pick
    corr_with    text,               -- that pick's alpha id
    members      text,               -- JSON [[alpha_id, corr], ...] folded into this pick
    status       text default 'proposed',   -- proposed | approved | rejected | submitted
    decided_at   timestamp, note text,
    updated_at   timestamp default current_timestamp);
"""

LOOKBACK_DAYS = 4 * 252


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def fetch_pnl(conn: sqlite3.Connection, brain: Brain, account: str, alpha_ids: list[str], workers: int = 3) -> int:
    ensure(conn)
    have = {r[0] for r in conn.execute("select alpha_id from pnl")}
    todo = [a for a in alpha_ids if a not in have]

    def get(aid):
        d = brain._poll(f"{BASE}/alphas/{aid}/recordsets/pnl")
        names = [p["name"] for p in d["schema"]["properties"]]
        i = names.index("pnl")
        recs = d.get("records", [])
        return aid, [r[0] for r in recs], [r[i] for r in recs]

    with ThreadPoolExecutor(workers) as ex:
        for aid, dates, cum in ex.map(get, todo):
            conn.execute("insert or replace into pnl(alpha_id, account, dates, cum) values (?,?,?,?)",
                         (aid, account, json.dumps(dates), json.dumps(cum)))
            conn.commit()
    return len(todo)


def daily(conn: sqlite3.Connection, alpha_id: str) -> dict[str, float]:
    row = conn.execute("select dates, cum from pnl where alpha_id=?", (alpha_id,)).fetchone()
    if not row:
        return {}
    dates, cum = json.loads(row[0]), json.loads(row[1])
    out = {}
    for k in range(1, len(dates)):
        if cum[k] is not None and cum[k - 1] is not None:
            out[dates[k]] = cum[k] - cum[k - 1]
    keep = sorted(out)[-LOOKBACK_DAYS:]
    return {d: out[d] for d in keep}


def corr(a: dict[str, float], b: dict[str, float]) -> float:
    common = a.keys() & b.keys()
    n = len(common)
    if n < 60:
        return 0.0
    xs = [a[d] for d in common]
    ys = [b[d] for d in common]
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    return sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else 0.0


def score(r: sqlite3.Row) -> float:
    """Sharpe, a bonus for single-dataset (Power Pool + ATOM, outside the quota),
    a penalty for turnover close to the 0.7 limit."""
    return (r["sharpe"] or 0) + 0.3 * (r["single_dataset"] or 0) - 0.5 * max(0.0, (r["turnover"] or 0) - 0.5)


def passing(conn: sqlite3.Connection, account: str) -> list[sqlite3.Row]:
    rows = conn.execute("""select r.*, c.single_dataset, c.datasets, c.categories, c.n_ops
                           from resims r join candidates c on c.id = r.candidate_id
                           where r.status='done' and r.account=? and r.alpha_id is not null""", (account,)).fetchall()
    return [r for r in rows if verdict(r["sharpe"], r["checks"])[0] == "pp_ok"]


def build(conn: sqlite3.Connection, account: str, threshold: float = 0.5) -> list[dict]:
    ensure(conn)
    rows = passing(conn, account)
    prior = {r["alpha_id"]: r["status"] for r in conn.execute("select alpha_id, status from picks where account=?", (account,))}
    decided_rank = {"submitted": 0, "approved": 1}
    rows.sort(key=lambda r: (decided_rank.get(prior.get(r["alpha_id"]), 2), -score(r)))
    series = {r["alpha_id"]: daily(conn, r["alpha_id"]) for r in rows}

    picks: list[dict] = []
    for r in rows:
        aid = r["alpha_id"]
        best = max(((abs(corr(series[aid], series[p["alpha_id"]])), p) for p in picks),
                   key=lambda t: t[0], default=(0.0, None))
        if best[1] is not None and best[0] >= threshold and prior.get(aid) not in decided_rank:
            best[1]["members"].append([aid, round(best[0], 3)])
            continue
        picks.append({"alpha_id": aid, "row": r, "members": [], "score": score(r)})

    # each pick's highest correlation with any other pick (all below threshold unless both were decided)
    for p in picks:
        others = [(abs(corr(series[p["alpha_id"]], series[q["alpha_id"]])), q["alpha_id"]) for q in picks if q is not p]
        p["max_corr"], p["corr_with"] = max(others, default=(0.0, None))

    keep = {p["alpha_id"] for p in picks}
    conn.execute("delete from picks where account=? and status='proposed' and alpha_id not in (%s)"
                 % ",".join("?" * len(keep) or ["''"]), (account, *keep))
    for p in picks:
        r = p["row"]
        conn.execute("""insert into picks(alpha_id, resim_id, candidate_id, account, cluster, score, max_corr, corr_with, members)
                        values (?,?,?,?,?,?,?,?,?)
                        on conflict(alpha_id) do update set cluster=excluded.cluster, score=excluded.score,
                          max_corr=excluded.max_corr, corr_with=excluded.corr_with, members=excluded.members,
                          updated_at=current_timestamp""",
                     (p["alpha_id"], r["id"], r["candidate_id"], account, r["id"], p["score"],
                      round(p["max_corr"], 3), p["corr_with"], json.dumps(p["members"])))
    conn.commit()
    return picks


def decide(conn: sqlite3.Connection, alpha_id: str, status: str, note: str | None = None) -> None:
    if status not in ("proposed", "approved", "rejected"):
        raise ValueError(status)
    ensure(conn)
    conn.execute("update picks set status=?, note=coalesce(?, note), decided_at=current_timestamp where alpha_id=?",
                 (status, note, alpha_id))
    conn.commit()
