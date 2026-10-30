"""Our strongest USA ideas, re-simulated in the other markets the account can trade.

Until 30 Oct the other markets only saw templates (EUR/ASI: 1,340 runs, 7 passed the
Power Pool rules) and a few Qwen ideas; none of our strong USA results was ever tried
there. A USA result qualifies when it is strong (Sharpe/fitness at the bar, plausible)
and BRAIN did not flag it for Power Pool correlation or concentrated weight. It is
copied to GLB, EUR and ASI when every field it uses is synced for the account in that
region, with the region's universe and neutralization (markets.py) and the parent's
decay and truncation. GLB copies suit themes like 'AMER Sharpe > 1': the USA stocks are
the Americas part of GLB.

Copies are candidates with status 'transfer' (Lane A); the account's own results only, so
the other account's lanes leave them alone (lanes.other_accounts: same field family).
30 Oct: of 105 (HT52122) and 184 (YO50689) qualifying results only 9 copy as they are,
all to EUR/ASI: most use fields those regions don't have (implied_volatility_*, some
anl4 estimates) or exceed the Power Pool limits (3 fields, 8 operators).
"""
from __future__ import annotations

import json
import sqlite3

from . import markets
from .pp import check
from .verdict import quality_gate

TARGETS = ("GLB", "EUR", "ASI")
MIN_SHARPE, MIN_FITNESS = 1.5, 1.0
STOCK = 15          # untested copies kept per account
PER_PASS = 6


def parents(conn: sqlite3.Connection, account: str) -> list[sqlite3.Row]:
    """The account's strong USA results that BRAIN did not flag for Power Pool correlation
    or concentration, strongest (Sharpe x fitness) first."""
    rows = conn.execute("""select r.*, c.fields from resims r join candidates c on c.id = r.candidate_id
                           where r.account=? and r.status='done' and r.settings like '%"USA"%'
                             and r.sharpe >= ? and r.fitness >= ?
                           order by r.sharpe * r.fitness desc""", (account, MIN_SHARPE, MIN_FITNESS)).fetchall()
    out = []
    for r in rows:
        ch = {c.get("name"): c.get("result") for c in json.loads(r["checks"] or "[]") or []}
        if ch.get("POWER_POOL_CORRELATION") in ("FAIL", "WARNING") or ch.get("CONCENTRATED_WEIGHT") in ("FAIL", "WARNING"):
            continue
        low = quality_gate(r["sharpe"], r["fitness"], r["turnover"], r["checks"], returns=r["returns"])
        if any("implausible" in x for x in low):
            continue
        out.append(r)
    return out


def _synced(conn, account: str, region: str) -> set[str]:
    return {f for (f,) in conn.execute("select field from account_fields where account=? and region=?", (account, region))}


def stock(conn, account: str) -> int:
    return conn.execute("""select count(*) from candidates c where c.status='transfer'
                           and c.sources like ? and not exists (select 1 from resims r where r.candidate_id=c.id)""",
                        (f'%@{account}"%',)).fetchone()[0]


def ideas(conn: sqlite3.Connection, catalog, account: str, targets=TARGETS, per_pass: int = PER_PASS) -> int:
    """Queue copies of the strongest untransferred USA results; keeps about STOCK untested."""
    if stock(conn, account) >= STOCK:
        return 0
    synced = {t: _synced(conn, account, t) for t in targets}
    added = 0
    for p in parents(conn, account):
        st = json.loads(p["settings"])
        for region in targets:
            if added >= per_pass:
                conn.commit()
                return added
            chk = check(p["expr"], catalog, region)
            if chk.problems or not set(chk.fields) <= synced[region]:
                continue
            cur = conn.execute(
                """insert or ignore into candidates(key, expr, region, delay, universe, neutralization, decay, truncation,
                       n_ops, n_fields, fields, categories, datasets, single_category, single_dataset, problems, n_seen,
                       sources, reasoning, family, flags, status, parent_id, depth)
                   values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'[]',1,?,?,?,'[]','transfer',?,1)""",
                (f"{region}|{st.get('delay', 1)}|{chk.canonical}", p["expr"], region, st.get("delay", 1),
                 markets.universe(region), markets.neutralization(region), st.get("decay"), st.get("truncation"),
                 chk.n_ops, len(chk.fields), json.dumps(chk.fields), json.dumps(chk.categories), json.dumps(chk.datasets),
                 int(len(chk.categories) == 1), int(len(chk.datasets) == 1),
                 json.dumps([f"transfer:{p['alpha_id']}@{account}"]),
                 f"USA {p['alpha_id']} sharpe {p['sharpe']} fitness {p['fitness']} in {region}",
                 f"{region}|{st.get('delay', 1)}|" + ",".join(sorted(chk.fields)), p["candidate_id"]))
            added += cur.rowcount
    conn.commit()
    return added
