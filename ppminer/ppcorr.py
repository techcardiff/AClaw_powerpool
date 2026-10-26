"""Power Pool correlation when BRAIN's /check never computes it.

For many alphas /check leaves SELF_CORRELATION 'PENDING' and has no
POWER_POOL_CORRELATION entry at all, even after correlations were requested.
GET /alphas/{id}/correlations/self does return a result: the TOP 5 most
correlated submitted alphas with their correlation. From that:

  * a Power Pool alpha among the top 5 -> its correlation is the answer
  * none among the top 5 -> Power Pool correlation <= the smallest value listed;
    if that bound is < 0.5 it passes, otherwise it stays unknown (skipped)

Which alphas count as Power Pool: the PowerPoolSelected alphas of ALL our accounts.
BRAIN's self-correlation for YO50689 lists HT52122's alphas (26 Oct), so the pools
are linked on BRAIN's side; counting both is the conservative choice.
When the top 5 can't settle it (all >= 0.5, all regular alphas), the correlation is
computed directly: daily PnL over the last 4 years against every Power Pool alpha
whose PnL we can read (this account's, plus any cached for the other account), the
same method as the pick list. A safety margin applies (OWN_LIMIT) because BRAIN's
exact method may differ slightly.
BRAIN still runs its own check at submission and refuses anything over the limit.
"""
from __future__ import annotations

import json
import sqlite3

from .brain import BASE, Brain

LIMIT = 0.5
OWN_LIMIT = 0.45


def pp_alpha_ids(conn: sqlite3.Connection) -> set[str]:
    return {aid for aid, tags in conn.execute("select alpha_id, tags from account_submitted")
            if "PowerPoolSelected" in json.loads(tags or "[]")}


def own_pnl_corr(brain: Brain, conn: sqlite3.Connection, account: str, alpha_id: str,
                 pp_ids: set[str]) -> tuple[float | None, str]:
    """Max |daily-PnL correlation| with the Power Pool alphas (4 years), fetching missing PnL
    for this account's own alphas. None if no Power Pool PnL is available."""
    from .picks import corr, daily, fetch_pnl
    own = {a for (a,) in conn.execute("select alpha_id from account_submitted where account=?", (account,))}
    fetch_pnl(conn, brain, account, [alpha_id] + sorted(pp_ids & own))
    cached = {a for (a,) in conn.execute("select alpha_id from pnl")}
    usable = sorted(pp_ids & cached)
    if not usable:
        return None, "no Power Pool PnL available"
    me = daily(conn, alpha_id)
    worst = max((abs(corr(me, daily(conn, p))), p) for p in usable)
    return worst[0], f"own daily-PnL correlation vs {len(usable)} Power Pool alphas (max with {worst[1]})"


def estimate(brain: Brain, alpha_id: str, pp_ids: set[str], conn: sqlite3.Connection | None = None,
             account: str | None = None) -> tuple[float | None, str]:
    """(value, how). value None = can't tell."""
    d = brain._poll(f"{BASE}/alphas/{alpha_id}/correlations/self", max_wait=300)
    names = [p["name"] for p in d["schema"]["properties"]]
    i_id, i_c = names.index("id"), names.index("correlation")
    recs = [(r[i_id], r[i_c]) for r in d.get("records", []) if r[i_c] is not None]
    if not recs:
        return 0.0, "no submitted alpha correlates"
    pp = [abs(c) for a, c in recs if a in pp_ids]
    if pp:
        return max(pp), "Power Pool alpha in the top 5 of correlations/self"
    bound = min(abs(c) for _, c in recs)
    if bound < LIMIT:
        return bound, "upper bound: no Power Pool alpha in the top 5 of correlations/self"
    if conn is not None and account:
        v, how = own_pnl_corr(brain, conn, account, alpha_id, pp_ids)
        if v is not None:
            # report as at/over the limit unless clearly under it (method may differ slightly from BRAIN's)
            return (v if v < OWN_LIMIT else max(v, LIMIT)), how
    return None, "top 5 correlations are all >= 0.5 and none is a Power Pool alpha"


def needs_fallback(checks: list[dict]) -> bool:
    ppc = next((c for c in checks or [] if c.get("name") == "POWER_POOL_CORRELATION"), None)
    return ppc is None or ppc.get("result") == "PENDING"


def fill(brain: Brain, conn: sqlite3.Connection, alpha_id: str, checks: list[dict],
         account: str | None = None) -> list[dict]:
    """Return checks with a POWER_POOL_CORRELATION entry from correlations/self when /check has none.
    The added entry carries "source" so it is never mistaken for BRAIN's own check."""
    if not needs_fallback(checks):
        return checks
    value, how = estimate(brain, alpha_id, pp_alpha_ids(conn), conn, account)
    if value is None:
        return checks
    out = [c for c in checks if c.get("name") != "POWER_POOL_CORRELATION"]
    out.append({"name": "POWER_POOL_CORRELATION", "result": "PASS" if value < LIMIT else "WARNING",
                "limit": LIMIT, "value": round(value, 4), "source": f"correlations/self ({how})"})
    return out
