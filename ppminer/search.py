"""Backend of the Search page (/search): funnel, region x dataset map, manual editor, templates.

Manual simulations only queue a job (lane 'M'); the scheduler runs it first in that
account's next batch, so the page never talks to BRAIN itself and never submits.
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter

from . import markets, templates
from .catalog import Catalog
from .pp import check
from .verdict import atom_verdict, regular_verdict, verdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG_DIR = os.path.join(ROOT, "data", "catalog")
SIMULATED_STATUSES = ("pp_ready", "pp_multi", "explore", "rewrite", "template", "funnel", "manual", "purify")

_verdicts: dict = {}          # (resim id, checks length) -> verdicts; checks only change on recheck
_catalogs: dict = {}


def catalog(conn: sqlite3.Connection, account: str) -> Catalog:
    if account not in _catalogs:
        c = Catalog(CATALOG_DIR)
        c.add_account_fields(conn, account)
        _catalogs[account] = c
    return _catalogs[account]


def verdicts(row) -> dict:
    key = (row["id"], len(row["checks"] or ""))
    if key not in _verdicts:
        pp, why = verdict(row["sharpe"], row["checks"])
        reg, fails = regular_verdict(row["checks"])
        atom, _ = atom_verdict(row["checks"], bool(row["single_dataset"]))
        _verdicts[key] = {"pp": pp, "pp_why": why, "regular": reg, "fails": fails, "atom": atom}
    return _verdicts[key]


def _done(conn, account: str, region: str | None):
    q = """select r.id, r.alpha_id, r.sharpe, r.fitness, r.turnover, r.checks, r.lane, r.purpose, r.expr,
                  c.single_dataset, c.region, c.datasets, c.categories
           from resims r join candidates c on c.id = r.candidate_id
           where r.account=? and r.status='done'""" + (" and c.region=?" if region else "")
    return conn.execute(q, (account, region) if region else (account,)).fetchall()


def funnel(conn: sqlite3.Connection, account: str, region: str | None = None) -> dict:
    rows = _done(conn, account, region)
    v = [(r, verdicts(r)) for r in rows]
    strong = [(r, x) for r, x in v if (r["sharpe"] or 0) >= 1]
    blocks = Counter(f for r, x in strong if x["regular"] != "reg_ok" for f in x["fails"])
    ideas = conn.execute(f"""select count(*) from candidates where status in ({','.join('?' * len(SIMULATED_STATUSES))})"""
                         + (" and region=?" if region else ""),
                         SIMULATED_STATUSES + ((region,) if region else ())).fetchone()[0]
    return {
        "stages": [
            ["ideas in the pool", ideas],
            ["simulated", len(rows)],
            ["sharpe >= 1", len(strong)],
            ["Power Pool OK", sum(x["pp"] == "pp_ok" for _, x in v)],
            ["regular near miss", sum(x["regular"] == "near" for _, x in v)],
            ["regular OK", sum(x["regular"] == "reg_ok" for _, x in v)],
            ["ATOM OK", sum(x["atom"] == "atom_ok" for _, x in v)],
        ],
        "blocking_checks": blocks.most_common(8),
        "by_lane": Counter(f"{r['lane'] or '-'} / {(r['purpose'] or '').split(':')[0]}" for r in rows).most_common(),
    }


def dataset_map(conn: sqlite3.Connection, account: str, region: str) -> list[dict]:
    stats: dict = {}
    for r in _done(conn, account, region):
        for ds in json.loads(r["datasets"] or "[]") or ["?"]:
            s = stats.setdefault(ds, {"sims": 0, "best": None, "pp_ok": 0, "near": 0, "atom_ok": 0})
            x = verdicts(r)
            s["sims"] += 1
            s["best"] = max(s["best"] or -9, r["sharpe"] or -9)
            s["pp_ok"] += x["pp"] == "pp_ok"
            s["near"] += x["regular"] == "near"
            s["atom_ok"] += x["atom"] == "atom_ok"
    out = []
    for d in conn.execute("""select dataset, name, category, field_count, value_score,
                                    (select count(*) from account_fields f where f.account=d.account and f.region=d.region
                                       and f.dataset=d.dataset) as synced
                             from account_datasets d where account=? and region=?""", (account, region)):
        out.append({"dataset": d["dataset"], "name": d["name"], "category": d["category"], "fields": d["field_count"],
                    "synced": d["synced"], "value": d["value_score"], **stats.pop(d["dataset"], {"sims": 0})})
    out += [{"dataset": k, "name": "", "category": "", **s} for k, s in stats.items()]
    return sorted(out, key=lambda x: (-(x.get("pp_ok") or 0), -(x.get("sims") or 0), -(x.get("value") or 0)))


def check_expr(conn, account: str, region: str, expr: str) -> dict:
    c = check(expr, catalog(conn, account), region)
    return {"parsed": c.parsed, "ops": c.n_ops, "fields": c.fields, "categories": c.categories, "datasets": c.datasets,
            "unknown": c.unknown, "problems": c.problems, "pp_shape": c.shape_ok}


def default_settings(region: str) -> dict:
    return {"instrumentType": "EQUITY", "region": region, "universe": markets.universe(region), "delay": 1, "decay": 4,
            "neutralization": markets.neutralization(region), "truncation": 0.08, "pasteurization": "ON",
            "unitHandling": "VERIFY", "nanHandling": "OFF", "language": "FASTEXPR", "visualization": False}


def queue_manual(conn, account: str, region: str, expr: str, overrides: dict | None = None) -> int:
    """Queue one expression for the scheduler (lane M, runs first in the account's next batch)."""
    c = check(expr, catalog(conn, account), region)
    if not c.parsed:
        raise ValueError("; ".join(c.problems))
    st = dict(default_settings(region), **{k: v for k, v in (overrides or {}).items() if v not in (None, "")})
    key = f"{region}|{st['delay']}|{c.canonical}"
    conn.execute("""insert or ignore into candidates(key, expr, region, delay, universe, neutralization, decay, truncation,
                        n_ops, n_fields, fields, categories, datasets, single_category, single_dataset, problems, n_seen,
                        sources, flags, status) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'["manual"]','[]','manual')""",
                 (key, expr, region, st["delay"], st["universe"], st["neutralization"], st["decay"], st["truncation"],
                  c.n_ops, len(c.fields), json.dumps(c.fields), json.dumps(c.categories), json.dumps(c.datasets),
                  int(c.single_category), int(c.single_dataset), json.dumps(c.problems)))
    cid = conn.execute("select id from candidates where key=?", (key,)).fetchone()[0]
    rid = conn.execute("""insert into resims(candidate_id, account, expr, settings, status, lane, purpose)
                          values (?,?,?,?,'queued','M','manual')""", (cid, account, expr, json.dumps(st))).lastrowid
    conn.commit()
    return rid


def manual_jobs(conn, account: str) -> list[dict]:
    out = []
    for r in conn.execute("""select r.id, r.alpha_id, r.status, r.sharpe, r.fitness, r.turnover, r.checks, r.expr, r.settings,
                                    r.error, r.created_at, r.lane, r.purpose, c.single_dataset
                             from resims r join candidates c on c.id = r.candidate_id
                             where r.account=? and r.lane='M' order by r.id desc limit 40""", (account,)):
        d = {k: r[k] for k in ("id", "alpha_id", "status", "sharpe", "fitness", "turnover", "expr", "error", "created_at")}
        d["settings"] = {k: v for k, v in json.loads(r["settings"] or "{}").items()
                         if k in ("region", "universe", "neutralization", "decay", "truncation", "delay")}
        if r["status"] == "done":
            d.update(verdicts(r))
            d["checks"] = [(c.get("name"), c.get("result"), c.get("value"), c.get("limit"))
                           for c in json.loads(r["checks"] or "[]") or []]
        out.append(d)
    return out


def datasets(conn, account: str, region: str) -> list[dict]:
    return [dict(r) for r in conn.execute("""
        select d.dataset, d.name, d.category, d.field_count,
               (select count(*) from account_fields f where f.account=d.account and f.region=d.region and f.dataset=d.dataset) synced
        from account_datasets d where d.account=? and d.region=? order by d.category, d.dataset""", (account, region))]


def template_preview(conn, account: str, region: str, kind: str, dataset: str | None, n_fields: int,
                     base_expr: str | None, order: str = "coverage", fields: list[str] | None = None) -> list[dict]:
    rows = templates.generate(conn, catalog(conn, account), account, region, kind, dataset, n_fields, base_expr,
                              order, fields)
    return [{k: r[k] for k in ("expr", "kind", "ops", "datasets", "problems")} for r in rows]


def template_queue(conn, account: str, region: str, kind: str, dataset: str | None, n_fields: int,
                   base_expr: str | None, order: str = "coverage", fields: list[str] | None = None) -> int:
    rows = templates.generate(conn, catalog(conn, account), account, region, kind, dataset, n_fields, base_expr,
                              order, fields)
    return templates.save(conn, region, rows, source=f"template:{kind}:{account}")


# ── Fields tab ──────────────────────────────────────────────────────────────
FIELD_SORTS = {
    "alphas": "coalesce(f.alpha_count, 0) desc",          # most used on the platform: proven, crowded
    "users": "coalesce(f.user_count, 0) desc",
    "coverage": "coalesce(f.coverage, 0) desc, coalesce(f.user_count, 0) desc",
    "fresh": "coalesce(f.alpha_count, 0) asc, coalesce(f.coverage, 0) desc",   # least used, decent coverage
}
FRESH_MIN_COVERAGE = 0.6


def _own_field_stats(conn, account: str, region: str) -> dict:
    """Per field: how often we simulated it on this account, best Sharpe, Power Pool OKs."""
    out: dict = {}
    for r in conn.execute("""select r.id, r.sharpe, r.checks, c.fields, c.single_dataset from resims r
                             join candidates c on c.id = r.candidate_id
                             where r.account=? and r.status='done' and c.region=?""", (account, region)):
        v = verdicts(r)
        for f in json.loads(r["fields"] or "[]"):
            s = out.setdefault(f, {"sims": 0, "best": None, "pp_ok": 0})
            s["sims"] += 1
            if r["sharpe"] is not None:
                s["best"] = r["sharpe"] if s["best"] is None else max(s["best"], r["sharpe"])
            s["pp_ok"] += v["pp"] == "pp_ok"
    return out


def fields(conn, account: str, region: str, dataset: str | None = None, category: str | None = None,
           ftype: str | None = None, min_cov: float = 0.0, q: str | None = None, sort: str = "alphas",
           limit: int = 300) -> dict:
    where, args = ["f.account=?", "f.region=?"], [account, region]
    if dataset:
        where.append("f.dataset=?"); args.append(dataset)
    if category:
        where.append("f.category=?"); args.append(category)
    if ftype:
        where.append("f.type=?"); args.append(ftype)
    if sort == "fresh":
        # least used, but used at least once: never-used fields are mostly metadata (flags, timestamps)
        min_cov = max(min_cov, FRESH_MIN_COVERAGE)
        where.append("coalesce(f.alpha_count, 0) >= 1")
    if min_cov:
        where.append("coalesce(f.coverage, 0) >= ?"); args.append(min_cov)
    if q:
        where.append("(f.field like ? or f.description like ?)"); args += [f"%{q}%", f"%{q}%"]
    total = conn.execute(f"select count(*) from account_fields f where {' and '.join(where)}", args).fetchone()[0]
    rows = conn.execute(f"""select f.field, f.dataset, f.category, f.type, f.coverage, f.user_count, f.alpha_count,
                                   f.description from account_fields f where {' and '.join(where)}
                            order by {FIELD_SORTS.get(sort, FIELD_SORTS['alphas'])} limit ?""", args + [limit]).fetchall()
    own = _own_field_stats(conn, account, region)
    blocked = {b for (b,) in conn.execute("select field from field_blocklist where account=?", (account,))}
    out = []
    for r in rows:
        d = dict(r)
        d.update(own.get(r["field"], {"sims": 0, "best": None, "pp_ok": 0}))
        d["blocked"] = r["field"] in blocked
        out.append(d)
    cats = [c for (c,) in conn.execute("select distinct category from account_fields where account=? and region=? "
                                       "order by 1", (account, region))]
    return {"total": total, "rows": out, "categories": cats}
