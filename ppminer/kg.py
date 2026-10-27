"""Knowledge graph of what our own simulations proved, condensed into hints for Qwen.

Like the old quant-agent graph (tools/knowledge_graph.py), it only learns from QUALITY
alphas, but it is built from pp.db: the harvested history (candidates, old Sharpe) and
our BRAIN results (resims). Per region:
  field  how often it appears in a proven alpha (|sharpe| >= PROVEN_SHARPE and
         |fitness| >= PROVEN_FITNESS), its proven rate and average proven |sharpe|
  pair   two fields co-occurring in proven alphas, with their average |sharpe|
Popularity is BRAIN's own count of platform alphas per field (account_fields.alpha_count).

HIDDEN GEMS = proven here, little used on the platform: important signal that few
other researchers' alphas (or our Power Pool) already hold, so low correlation.
Regions with little history (EUR/ASI/GLB) borrow USA evidence for fields with the same
id there, marked as proven in USA, untested in the region; for the GLB theme
(AMER Sharpe > 1) USA-proven signal is the natural starting point.
"""
from __future__ import annotations

import bisect
import json
import sqlite3
import time
from collections import defaultdict
from itertools import combinations

from . import hints
from .harvest import SUSPICIOUS_SHARPE

PROVEN_SHARPE = 1.25
PROVEN_FITNESS = 0.8
MIN_PROVEN = 3              # a field needs this many proven alphas to count as proven
GEM_POPULARITY = 0.4        # gems: the less-used 40% of the region's proven fields on the platform
HUB_POPULARITY = 0.95       # hubs: the 5% most-used fields on the platform
REBUILD_SECONDS = 6 * 3600

_CACHE: dict = {"at": 0.0, "graph": None}


def _evidence(conn: sqlite3.Connection):
    """(region, fields, |sharpe|, |fitness|) of every simulated alpha we know of."""
    for region, fields, sh, fi in conn.execute(
            """select region, fields, sharpe, fitness from candidates where sharpe is not null"""):
        yield region, fields, sh, fi
    for region, fields, sh, fi in conn.execute(
            """select k.region, k.fields, r.sharpe, r.fitness from resims r join candidates k on k.id = r.candidate_id
               where r.status='done' and r.sharpe is not null"""):
        yield region, fields, sh, fi


def build(conn: sqlite3.Connection) -> dict:
    """{region: {"fields": {f: [n, n_proven, sum_sharpe]}, "pairs": {(a, b): [n, sum_sharpe]}}}"""
    g: dict = defaultdict(lambda: {"fields": defaultdict(lambda: [0, 0, 0.0]), "pairs": defaultdict(lambda: [0, 0.0])})
    for region, fields, sh, fi in _evidence(conn):
        sh, fi = abs(sh or 0), abs(fi or 0)
        if sh >= SUSPICIOUS_SHARPE:
            continue                        # degenerate history would teach garbage
        fs = sorted(set(json.loads(fields or "[]")))
        proven = sh >= PROVEN_SHARPE and fi >= PROVEN_FITNESS
        reg = g[region or "USA"]
        for f in fs:
            st = reg["fields"][f]
            st[0] += 1
            if proven:
                st[1] += 1
                st[2] += sh
        if proven:
            for a, b in combinations(fs, 2):
                p = reg["pairs"][(a, b)]
                p[0] += 1
                p[1] += sh
    return g


def graph(conn: sqlite3.Connection) -> dict:
    if _CACHE["graph"] is None or time.time() - _CACHE["at"] > REBUILD_SECONDS:
        _CACHE["graph"], _CACHE["at"] = build(conn), time.time()
    return _CACHE["graph"]


def _popularity(conn, account: str, region: str) -> dict[str, tuple[float, str, int]]:
    """field -> (percentile of platform alpha_count within the region, dataset, alpha_count)."""
    rows = conn.execute("""select field, dataset, coalesce(alpha_count, 0) from account_fields
                           where account=? and region=? and delay=1""", (account, region)).fetchall()
    if not rows:
        return {}
    order = sorted(r[2] for r in rows)
    n = len(order)
    return {f: (bisect.bisect_left(order, c) / n, ds, c) for f, ds, c in rows}


def field_table(conn: sqlite3.Connection, account: str, region: str) -> list[dict]:
    """Every proven field the account can use in the region, with its evidence and popularity.
    Evidence comes from the region itself, or from USA for a field id that exists there too."""
    g = graph(conn)
    pop = _popularity(conn, account, region)
    out = []
    for f, (pct, ds, count) in pop.items():
        for src in ([region, "USA"] if region != "USA" else ["USA"]):
            st = g.get(src, {}).get("fields", {}).get(f)
            if st and st[1] >= MIN_PROVEN:
                out.append({"field": f, "dataset": ds, "platform_alphas": count, "evidence": src,
                            "n": st[0], "proven": st[1], "rate": st[1] / st[0], "avg_sharpe": st[2] / st[1]})
                break
    # popularity among the PROVEN fields: most fields have no platform alphas at all, so
    # against all fields nearly every proven field would look popular
    order = sorted(r["platform_alphas"] for r in out)
    for r in out:
        r["popularity"] = bisect.bisect_left(order, r["platform_alphas"]) / len(order)
    return out


def _stem(field: str) -> str:
    """Same family rule as select._stems: horizon variants share everything but the last segment."""
    return field.rsplit("_", 1)[0] if "_" in field else field


def gems(conn, account: str, region: str, dataset: str | None = None, exclude: set[str] = frozenset(),
         k: int = 10) -> list[dict]:
    """Proven but little-used fields, strongest evidence first (proven rate x average Sharpe).
    `exclude`: field families (stems) already in our submitted alphas."""
    rows = [r for r in field_table(conn, account, region)
            if r["popularity"] <= GEM_POPULARITY and _stem(r["field"]) not in exclude
            and (dataset is None or r["dataset"] == dataset)]
    return sorted(rows, key=lambda r: -(r["rate"] * r["avg_sharpe"] * min(r["proven"], 20) ** 0.5))[:k]


def proven_pairs(conn, region: str, fields: set[str], k: int = 6,
                 exclude: list[tuple[str, int]] = ()) -> list[tuple[str, str, int, float]]:
    """Proven pairs that involve any of `fields` (USA evidence for other regions); pairs
    with a field overused in our submitted alphas (`exclude`, hints.overused) are left out."""
    g = graph(conn)
    out = []
    for src in dict.fromkeys([region, "USA"]):
        for (a, b), (n, s) in g.get(src, {}).get("pairs", {}).items():
            if n >= 2 and (a in fields or b in fields) and not hints.is_overused(a, exclude) \
                    and not hints.is_overused(b, exclude):
                out.append((a, b, n, s / n))
        if out:
            break
    return sorted(out, key=lambda p: -(p[3] * min(p[2], 20) ** 0.5))[:k]


def hubs(conn, account: str, region: str, dataset: str | None = None, k: int = 6) -> list[str]:
    """The most used fields on the platform (crowded: high correlation risk)."""
    pop = _popularity(conn, account, region)
    rows = [(c, f) for f, (pct, ds, c) in pop.items() if pct >= HUB_POPULARITY and (dataset is None or ds == dataset)]
    return [f for _, f in sorted(rows, reverse=True)[:k]]


def _fmt(r: dict, region: str) -> str:
    where = "" if r["evidence"] == region else " (proven in USA, untested here)"
    return (f"  {r['field']} [{r['dataset']}] proven {r['proven']}/{r['n']}, avg |sharpe| {r['avg_sharpe']:.2f}, "
            f"used by {r['platform_alphas']} platform alphas{where}")


def block(conn, account: str, region: str, dataset: str | None = None, avoid: set[str] = frozenset(),
          overused: list[tuple[str, int]] = ()) -> str:
    """Prompt section: hidden gems (this dataset first, then partners from other datasets),
    their proven pairs, and crowded hubs to avoid. Families in `avoid` (our submitted
    alphas' field stems) are left out: they would fail Power Pool correlation; pairs and
    crowded fields leave out the `overused` families too."""
    lines = [f"[KNOWLEDGE GRAPH - what our own simulations proved, {region}]"]
    here = gems(conn, account, region, dataset, avoid, k=8) if dataset else []
    partners = [r for r in gems(conn, account, region, None, avoid, k=14) if r["field"] not in {x["field"] for x in here}][:8]
    if here:
        lines.append(f" HIDDEN GEMS in {dataset} (proven, little used on the platform - build on these):")
        lines += [_fmt(r, region) for r in here]
    if partners:
        lines.append(" PROVEN PARTNERS from other datasets (pair ONE with a field of this dataset):")
        lines += [_fmt(r, region) for r in partners]
    pairs = proven_pairs(conn, region, {r["field"] for r in here + partners}, exclude=overused)
    if pairs:
        lines.append(" PROVEN PAIRS (co-occur in our quality alphas):")
        lines += [f"  {a} + {b} (n={n}, avg |sharpe| {s:.2f})" for a, b, n, s in pairs]
    crowded = [f for f in hubs(conn, account, region, dataset) if not hints.is_overused(f, overused)]
    if crowded:
        lines.append(f" CROWDED (most used on the platform - high correlation risk, avoid): {', '.join(crowded)}")
    return "\n".join(lines) + "\n" if len(lines) > 1 else ""
