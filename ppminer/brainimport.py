"""Import an account's unsubmitted alphas from BRAIN as candidates, for a stand-in submitter.

The 3080 keeps the miner's history (pp.db). When the cluster is unreachable, the lab workstation
can run the same scheduler from an empty database: every alpha the 3080 simulated still sits on
BRAIN as an unsubmitted alpha of its account, so this pulls the strongest ones back in
  * candidates  (key region|delay|canonical, the Power Pool shape from pp.check)
  * resims      (status done, lane 'I', checks from the alpha's in-sample checks)
and the usual after_batch -> picks -> auto-submit path does the rest (fresh BRAIN check,
Power Pool correlation, quality bar, quota). Nothing here submits.

    python -m ppminer import-brain [--per-account 40] [--accounts HT52122 YO50689]
"""
from __future__ import annotations

import json
import sqlite3

from . import cookies as ck, lanes
from .brain import BASE, Brain
from .catalog import Catalog
from .pp import check
from .verdict import QUALITY_DEFAULTS

PAGE = 100
MAX_PAGES = 40                       # 4,000 alphas per account at most, best Sharpe first
HARD_FAILS = {"LOW_SUB_UNIVERSE_SHARPE", "CONCENTRATED_WEIGHT", "LOW_TURNOVER", "HIGH_TURNOVER"}


def strong(alpha: dict, bar: dict = QUALITY_DEFAULTS) -> bool:
    """In-sample numbers and checks good enough to be worth a fresh BRAIN check."""
    is_ = alpha.get("is") or {}
    sh, fit, to = is_.get("sharpe"), is_.get("fitness"), is_.get("turnover")
    if sh is None or fit is None or to is None:
        return False
    if sh < bar.get("min_sharpe", 1.5) or fit < bar.get("min_fitness", 1.0) or to > bar.get("max_turnover", 0.4):
        return False
    fails = {c.get("name") for c in is_.get("checks") or [] if c.get("result") == "FAIL"}
    return not (fails & HARD_FAILS)


def unsubmitted(brain: Brain, max_pages: int = MAX_PAGES):
    """The account's unsubmitted alphas, best in-sample Sharpe first."""
    for page in range(max_pages):
        j = brain._req("GET", f"{BASE}/users/self/alphas",
                       params={"limit": PAGE, "offset": page * PAGE, "status": "UNSUBMITTED",
                               "order": "-is.sharpe", "hidden": "false"}).json()
        res = j.get("results") or []
        yield from res
        if len(res) < PAGE or (page + 1) * PAGE >= j.get("count", 0):
            return


def save(conn: sqlite3.Connection, catalog: Catalog, account: str, alpha: dict) -> bool:
    """One alpha as candidate + done resim. False when it isn't a Power Pool shape or is already in."""
    aid = alpha["id"]
    if conn.execute("select 1 from resims where alpha_id=?", (aid,)).fetchone():
        return False
    expr = ((alpha.get("regular") or {}).get("code") or "").strip()
    st = alpha.get("settings") or {}
    region, delay = st.get("region") or "USA", int(st.get("delay", 1))
    if not expr:
        return False
    chk = check(expr, catalog, region)
    if not chk.shape_ok or chk.problems or chk.unknown_ops:
        return False
    is_ = alpha.get("is") or {}
    key = f"{region}|{delay}|{chk.canonical}"
    conn.execute(
        """insert or ignore into candidates(key, expr, region, delay, universe, neutralization, decay, truncation,
               n_ops, n_fields, fields, categories, datasets, single_category, single_dataset, problems,
               sharpe, fitness, turnover, returns, drawdown, n_seen, sources, best_alpha_id, reasoning, family, flags, status)
           values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'[]',?,?,?,?,?,1,?,?,?,?,'[]','imported')""",
        (key, expr, region, delay, st.get("universe"), st.get("neutralization"), st.get("decay"), st.get("truncation"),
         chk.n_ops, len(chk.fields), json.dumps(chk.fields), json.dumps(chk.categories), json.dumps(chk.datasets),
         int(chk.single_category), int(chk.single_dataset),
         is_.get("sharpe"), is_.get("fitness"), is_.get("turnover"), is_.get("returns"), is_.get("drawdown"),
         json.dumps([f"brain:{aid}@{account}"]), aid, "imported from BRAIN (stand-in submitter)",
         f"{region}|{delay}|" + ",".join(sorted(chk.fields))))
    cid = conn.execute("select id from candidates where key=?", (key,)).fetchone()[0]
    conn.execute("""insert into resims(candidate_id, account, expr, settings, alpha_id, sharpe, fitness, turnover,
                        returns, drawdown, checks, status, lane) values (?,?,?,?,?,?,?,?,?,?,?,'done','I')""",
                 (cid, account, expr, json.dumps(st), aid, is_.get("sharpe"), is_.get("fitness"), is_.get("turnover"),
                  is_.get("returns"), is_.get("drawdown"), json.dumps(is_.get("checks") or [])))
    conn.commit()
    return True


def run(conn: sqlite3.Connection, catalog: Catalog, account: str, per_account: int = 40,
        brain: Brain | None = None, bar: dict = QUALITY_DEFAULTS) -> dict:
    lanes.migrate(conn)
    brain = brain or Brain.from_cookie(ck.load_cookie(account))
    if not brain.is_authenticated():
        return {"status": "waiting_login"}
    seen = added = 0
    for a in unsubmitted(brain):
        seen += 1
        if (a.get("is") or {}).get("sharpe") is not None and a["is"]["sharpe"] < bar.get("min_sharpe", 1.5):
            break                                   # sorted by Sharpe: the rest are weaker
        if strong(a, bar) and save(conn, catalog, account, a):
            added += 1
            if added >= per_account:
                break
    return {"status": "done", "seen": seen, "added": added}
