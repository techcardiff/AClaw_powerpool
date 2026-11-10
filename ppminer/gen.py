"""Lane A ideas without Qwen: fixed expression shapes on fields nobody has tried.

Qwen stopped answering on 8 Nov and mining stopped with it: every template had been
simulated by one of the two accounts and the funnel had no near misses left. Yet ~97% of
the well-covered GLB fields had never appeared in any idea (Qwen read ~40 fields per
dataset). This fills Lane A's explore queue from them:

  1. seeds: one shape per untried field, the shape rotating so a batch spreads over
     shapes and datasets (shortlist caps both at 3), datasets the other account
     simulates in that region left to it, proven theme datasets first
  2. expansion: a seed with |sharpe| >= EXPAND_AT gets the other shapes, in the
     direction its sharpe showed (negative -> the whole signal flipped)

Ideas are saved as status 'explore' with source 'gen:...', so lane_a, the theme share,
the funnel, picks, descriptions and auto-submit treat them like Qwen's.
"""
from __future__ import annotations

import json
import re
import sqlite3

from . import hints, lanes, markets
from .pp import check
from .select import _stems

# (name, template). {f} is the field, already wrapped (vec_avg / ts_backfill) as needed.
SHAPES = [
    ("level", "rank(ts_mean({f}, 60))"),
    ("change", "rank(ts_delta({f}, 20))"),
    ("peer_z", "group_rank(ts_zscore({f}, 120), industry)"),
    ("history", "rank(ts_rank({f}, 250))"),
    ("stability", "-rank(ts_std_dev({f}, 60))"),
    ("recent", "ts_decay_linear(rank({f}), 10)"),
    ("gap", "zscore(ts_av_diff({f}, 120))"),
    ("peer_level", "group_rank({f}, subindustry)"),
]
MIN_COVERAGE = 0.5
SPARSE_BELOW = 0.8          # wrap in ts_backfill below this coverage
EXPAND_AT = 0.5
SEEDS_PER_PASS = 30
MIN_QUEUE = 25
# beside Qwen (about 25-35 Qwen ideas a pass, batches of 40): expansions first, a few seeds
SEEDS_ALONGSIDE, THEME_SEEDS_ALONGSIDE, EXPAND_ALONGSIDE = 3, 6, 2
# identifiers and labels, not quantities (premap_instrument_to_ticker_map_twn, benchmark_currency_code_2)
NOT_A_QUANTITY = re.compile(r"(^|_)(code|codes|map|ticker|tickers|id|ids|isin|cusip|sedol|currency|name|names|"
                            r"date|dates|flag|type|types|country|exchange|sector|industry|gics|identifier)(_|$|\d)")


def _operand(field: str, ftype: str, coverage: float | None) -> str:
    x = f"vec_avg({field})" if ftype == "VECTOR" else field
    return f"ts_backfill({x}, 60)" if (coverage or 0) < SPARSE_BELOW else x


def _flip(expr: str) -> str:
    return expr[1:] if expr.startswith("-") else f"-{expr}"


def _used_fields(conn, region: str) -> set[str]:
    used: set[str] = set()
    for (f,) in conn.execute("select fields from candidates where region=?", (region,)):
        used |= set(json.loads(f or "[]"))
    return used


def _others_datasets(conn, account: str, others, region: str) -> set[str]:
    """Datasets other accounts have simulated or queued in this region: left to them."""
    out: set[str] = set()
    for o in others:
        if o == account:
            continue
        for (ds,) in conn.execute("""select distinct k.datasets from resims r join candidates k on k.id = r.candidate_id
                                     where r.account=? and k.region=?""", (o, region)):
            out |= set(json.loads(ds or "[]"))
    return out


def _dataset_order(conn, account: str, region: str, theme) -> list[str]:
    from .runner import proven_theme_datasets
    first = [r["dataset"] for r in proven_theme_datasets(conn, account, theme, region)] if theme is not None else []
    rest = [r[0] for r in conn.execute("""select dataset from account_datasets where account=? and region=? and delay=1
                                          order by coalesce(value_score,0) desc, coalesce(pyramid_multiplier,1) desc,
                                                   coalesce(alpha_count,0) asc""", (account, region))]
    return list(dict.fromkeys(first + rest))


def _save(conn, catalog, account: str, region: str, expr: str, source: str, note: str) -> int:
    chk = check(expr, catalog, region)
    if not chk.shape_ok or chk.problems or chk.unknown_ops:
        return 0
    cur = conn.execute(
        """insert or ignore into candidates(key, expr, region, delay, universe, neutralization, decay, truncation,
               n_ops, n_fields, fields, categories, datasets, single_category, single_dataset, problems,
               n_seen, sources, reasoning, family, flags, status)
           values (?,?,?,1,?,?,4,0.08,?,?,?,?,?,?,?,'[]',1,?,?,?,'[]','explore')""",
        (f"{region}|1|{chk.canonical}", expr, region, markets.universe(region), markets.neutralization(region),
         chk.n_ops, len(chk.fields), json.dumps(chk.fields), json.dumps(chk.categories), json.dumps(chk.datasets),
         int(chk.single_category), int(chk.single_dataset), json.dumps([source]), note,
         f"{region}|1|" + ",".join(sorted(chk.fields))))
    return cur.rowcount


def seeds(conn, catalog, account: str, region: str, others=(), theme=None, n: int = SEEDS_PER_PASS) -> int:
    """One idea each for up to n untried fields, one field per dataset per round."""
    used = _used_fields(conn, region)
    theirs = _others_datasets(conn, account, others, region)
    pp, _ = lanes.collide_stems(conn, account)
    _, o_stems, _ = lanes.other_accounts(conn, account, list(others))
    blocked = pp | o_stems
    over = hints.overused(conn)
    per_ds: dict[str, list] = {}
    for r in conn.execute("""select field, dataset, type, coverage from account_fields
                             where account=? and region=? and delay=1 and type in ('MATRIX','VECTOR')
                               and coalesce(coverage,0) >= ? order by coverage desc""", (account, region, MIN_COVERAGE)):
        if (r[0] in used or r[1] in theirs or NOT_A_QUANTITY.search(r[0]) or _stems([r[0]]) in blocked or hints.is_overused(r[0], over)):
            continue
        per_ds.setdefault(r[1], []).append(r)
    order = [d for d in _dataset_order(conn, account, region, theme) if d in per_ds]
    added, i = 0, 0
    while added < n and any(per_ds[d] for d in order):
        for d in order:
            if added >= n:
                break
            if not per_ds[d]:
                continue
            f, ds, ftype, cov = per_ds[d].pop(0)
            name, tpl = SHAPES[i % len(SHAPES)]
            i += 1
            added += _save(conn, catalog, account, region, tpl.format(f=_operand(f, ftype, cov)),
                           f"gen:{ds}:{name}:seed", f"shape '{name}' on untried field {f} ({ds})")
    conn.commit()
    return added


def expand(conn, catalog, account: str, region: str, limit: int = 10) -> int:
    """Seeds with some signal get every other shape, signed by their sharpe."""
    rows = conn.execute("""select c.id, c.fields, c.sources, r.sharpe from candidates c join resims r on r.candidate_id = c.id
                           where r.account=? and r.status='done' and c.region=? and c.sources like '%gen:%:seed%'
                             and abs(r.sharpe) >= ? order by abs(r.sharpe) desc""", (account, region, EXPAND_AT)).fetchall()
    added, done = 0, 0
    for cid, fields, sources, sharpe in rows:
        f = json.loads(fields)[0]
        if conn.execute("select 1 from candidates where sources like ? limit 1", (f'%gen:%:expand:{cid}"%',)).fetchone():
            continue
        r = conn.execute("""select type, coverage, dataset from account_fields where account=? and region=? and field=?""",
                         (account, region, f)).fetchone()
        if r is None:
            continue
        seed_shape = json.loads(sources)[0].split(":")[2]
        for name, tpl in SHAPES:
            if name == seed_shape:
                continue
            expr = tpl.format(f=_operand(f, r[0], r[1]))
            if sharpe < 0:
                expr = _flip(expr)
            added += _save(conn, catalog, account, region, expr, f"gen:{r[2]}:{name}:expand:{cid}",
                           f"shape '{name}' on {f} ({r[2]}); its '{seed_shape}' seed had sharpe {sharpe}")
        done += 1
        if done >= limit:
            break
    conn.commit()
    return added


def top_up(conn, catalog, account: str, regions, others=(), theme=None, min_queue: int = MIN_QUEUE) -> dict:
    """Qwen down: expand promising seeds, then add seeds where Lane A has fewer than
    min_queue usable ideas. The theme's regions go first."""
    return _run(conn, catalog, account, regions, others, theme, min_queue=min_queue)


def alongside(conn, catalog, account: str, regions, others=(), theme=None) -> dict:
    """Qwen up: a smaller steady share beside Qwen's ideas. From 8 to 10 Nov, 1,485 shape
    ideas passed the Power Pool rules 2.6% of the time, against 3.1% for Qwen's explore ideas."""
    return _run(conn, catalog, account, regions, others, theme, min_queue=None,
                n_seeds=SEEDS_ALONGSIDE, theme_seeds=THEME_SEEDS_ALONGSIDE, expand_limit=EXPAND_ALONGSIDE)


def _run(conn, catalog, account, regions, others, theme, *, min_queue, n_seeds=SEEDS_PER_PASS,
         theme_seeds=SEEDS_PER_PASS, expand_limit=10) -> dict:
    first = sorted(theme.regions) if theme is not None and theme.regions else []
    out = {}
    for region in list(dict.fromkeys(first + list(regions))):
        n = expand(conn, catalog, account, region, limit=expand_limit)
        if min_queue is None or lanes.usable_ideas(conn, account, others).get(region, 0) < min_queue:
            n += seeds(conn, catalog, account, region, others, theme if region in first else None,
                       n=theme_seeds if region in first else n_seeds)
        if n:
            out[region] = n
    return out
