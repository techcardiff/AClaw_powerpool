"""Pick a diverse shortlist for a theme from harvested candidates."""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from itertools import zip_longest

from .expr import skeleton
from .theme import Theme


def shortlist(conn: sqlite3.Connection, theme: Theme | None, n: int = 40, *, statuses=("pp_ready",),
              max_per_dataset: int = 10, max_per_shape: int = 3, account: str | None = None,
              skip_done: bool = False, exclude_stems: set[str] | None = None,
              min_sharpe: float | None = None, min_fitness: float | None = None,
              exclude_exprs: set[str] | None = None, max_sharpe: float | None = None,
              skip_ids: set[int] | None = None) -> list[sqlite3.Row]:
    """Greedy: best |sharpe| first, one per family, capped per dataset and per
    operator shape so the list spreads across data and ideas.
    Single-dataset (Power Pool + ATOM, outside the quota) ranks ahead.
    theme=None is Lane A: any category/region (pure PP needs no theme in weeks 1-6).
    With an account, only alphas whose fields that account can use are picked."""
    from .fields import available
    q = f"""select * from candidates where status in ({','.join('?' * len(statuses))})
            and (family_rank = 1 or status in ('rewrite', 'explore'))
            order by single_dataset desc, abs(coalesce(sharpe, 0)) desc, id"""
    done = set()
    if account and skip_done:
        done = {r[0] for r in conn.execute("select candidate_id from resims where account=?", (account,))}
    done = done | (skip_ids or set())
    allowed_cache: dict = {}
    per_ds, per_shape, twins = Counter(), Counter(), set()
    proven, fresh_by_region = [], {}
    for row in conn.execute(q, statuses):
        if row["id"] in done:
            continue
        if min_sharpe is not None and abs(row["sharpe"] or 0) < min_sharpe:
            continue
        if min_fitness is not None and abs(row["fitness"] or 0) < min_fitness:
            continue
        if max_sharpe is not None and abs(row["sharpe"] or 0) >= max_sharpe:
            continue                      # old-pipeline leakage / data bugs
        # families already known to collide (folded into a submitted alpha, or the
        # account's own submitted alphas) would only fail correlation again
        if exclude_stems and _stems(json.loads(row["fields"])) in exclude_stems:
            continue
        if exclude_exprs and row["expr"].replace(" ", "") in exclude_exprs:
            continue
        cats = set(json.loads(row["categories"]))
        if theme and not theme.matches(delay=row["delay"], region=row["region"], categories=cats):
            continue
        if account:
            k = (row["region"], row["delay"])
            if k not in allowed_cache:
                allowed_cache[k] = available(conn, account, *k)
            allowed = allowed_cache[k]
            if allowed is None or not set(json.loads(row["fields"])) <= allowed:
                continue
        ds = ",".join(json.loads(row["datasets"]))
        shape = _shape(row["skeleton"] or skeleton(row["expr"], lambda f: f))
        twin = (shape, _stems(json.loads(row["fields"])))
        is_new = row["sharpe"] is None           # explore/rewrite ideas, never simulated
        if is_new and len(fresh_by_region.get(row["region"], [])) >= n:
            continue
        cap = 2 if is_new else max_per_dataset   # spread untested ideas over many datasets
        if per_ds[ds] >= cap or per_shape[(is_new, shape)] >= max_per_shape or twin in twins:
            continue
        per_ds[ds] += 1
        per_shape[(is_new, shape)] += 1
        twins.add(twin)
        if is_new:
            fresh_by_region.setdefault(row["region"], []).append(row)
        else:
            proven.append(row)
        if len(proven) >= n and sum(map(len, fresh_by_region.values())) >= 3 * n:
            break
    # untested ideas round-robin across regions: otherwise the large USA backlog
    # (older ids) always wins and EUR/ASI ideas never get simulated
    fresh = [r for group in zip_longest(*fresh_by_region.values()) for r in group if r is not None]
    # 2 proven (old sharpe) : 1 fresh idea, so new data gets simulated every batch
    out, pi, fi = [], iter(proven), iter(fresh)
    while len(out) < n:
        batch = [next(pi, None), next(pi, None), next(fi, None)]
        if all(b is None for b in batch):
            break
        out += [b for b in batch if b is not None]
    return out[:n]


def _stems(fields: list[str]) -> str:
    """Field names minus their last segment: mdl53_jc5_6month and mdl53_jc5_1year
    are horizon variants of one signal and should not both be picked."""
    return ",".join(sorted({f.rsplit("_", 1)[0] if "_" in f else f for f in fields}))


def _shape(sk: str | None) -> str:
    """Operator sequence only, e.g. 'ts_rank(divide(' — fields and numbers dropped."""
    if not sk:
        return ""
    out, word = [], ""
    for ch in sk:
        if ch.isalnum() or ch == "_":
            word += ch
        else:
            if ch == "(" and word:
                out.append(word)
            word = ""
    return ">".join(out)
