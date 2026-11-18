"""Import an account's unsubmitted alphas from BRAIN as candidates, for a stand-in submitter.

The 3080 keeps the miner's history (pp.db). When the cluster is unreachable, the lab workstation
can run the same scheduler from an empty database: every alpha the 3080 simulated still sits on
BRAIN as an unsubmitted alpha of its account, so this pulls the strongest ones back in
  * candidates  (key region|delay|canonical, the Power Pool shape from pp.check)
  * resims      (status done, lane 'I', checks from the alpha's in-sample checks)
and the usual after_batch -> picks -> auto-submit path does the rest (fresh BRAIN check,
Power Pool correlation, quality bar, quota). Nothing here submits.

    python -m ppminer import-brain [--per-account 40] [--account HT52122]...
    python -m ppminer import-brain --scope EUR/1 --scope USA/0 --account HT52122 --per-account 25 --pp-bar --regular

Scopes (region/delay): Osmosis only counts a scope once an account has 10 submitted alphas in it,
so --scope pulls in the account's own alphas of the scopes it is short in (the autosubmit order
puts them first, accounts.<id>.scope_targets). --pp-bar imports at the Power Pool bar (theme
alphas are judged against THEME_BAR later), and --regular queues the scope's alphas that pass
the regular bar but not the Power Pool shape for the regular lane (regular.py).
"""
from __future__ import annotations

import json
import sqlite3

import requests

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


PP_BAR = {"min_sharpe": 1.2, "min_fitness": 0.8, "max_turnover": 0.4}


def unsubmitted(brain: Brain, max_pages: int = MAX_PAGES, region: str | None = None, delay: int | None = None):
    """The account's unsubmitted alphas, best in-sample Sharpe first, optionally of one scope.
    BRAIN refuses offsets past about 1,000 (18 Nov: 400 at offset 1,100), which ends the listing."""
    params = {"limit": PAGE, "status": "UNSUBMITTED", "order": "-is.sharpe", "hidden": "false"}
    if region:
        params["settings.region"] = region
    if delay is not None:
        params["settings.delay"] = delay
    for page in range(max_pages):
        try:
            j = brain._req("GET", f"{BASE}/users/self/alphas", params={**params, "offset": page * PAGE}).json()
        except requests.HTTPError:
            if page == 0:
                raise
            return
        res = j.get("results") or []
        yield from res
        if len(res) < PAGE or (page + 1) * PAGE >= j.get("count", 0):
            return


def parse_scope(scope: str) -> tuple[str, int]:
    """'EUR/1' -> ('EUR', 1); a bare region means delay 1."""
    region, _, delay = scope.partition("/")
    return region.upper(), int(delay or 1)


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


def queue_regular(conn: sqlite3.Connection, catalog: Catalog, account: str, alpha: dict, origin: str,
                  any_shape: bool = False) -> bool:
    """An alpha for the regular lane: strong at the regular bar. Normally not a Power Pool shape (that
    goes through autosubmit instead). any_shape: Power Pool shapes too, for slots the Power Pool quota
    leaves free (BRAIN allows 4 submissions a day in all); the regular lane skips any alpha that
    autosubmit already submitted."""
    from . import regular
    st = alpha.get("settings") or {}
    expr = ((alpha.get("regular") or {}).get("code") or "").strip()
    chk = check(expr, catalog, st.get("region") or "USA")
    if not expr or (not any_shape and chk.shape_ok and not chk.problems and not chk.unknown_ops):
        return False
    if conn.execute("select 1 from account_submitted where account=? and alpha_id=?", (account, alpha["id"])).fetchone():
        return False
    is_ = alpha.get("is") or {}
    return regular.add(conn, [{"account": account, "alpha_id": alpha["id"], "expr": expr, "settings": st,
                               "origin": origin, "note": "own unsubmitted alpha, regular bar, BRAIN in-sample",
                               "sharpe": is_.get("sharpe"), "fitness": is_.get("fitness"),
                               "turnover": is_.get("turnover")}]) > 0


def run(conn: sqlite3.Connection, catalog: Catalog, account: str, per_account: int = 40,
        brain: Brain | None = None, bar: dict = QUALITY_DEFAULTS, scope: str | None = None,
        regular_too: bool = False, per_regular: int = 10, any_shape: bool = False) -> dict:
    lanes.migrate(conn)
    brain = brain or Brain.from_cookie(ck.load_cookie(account))
    if not brain.is_authenticated():
        return {"status": "waiting_login"}
    region, delay = parse_scope(scope) if scope else (None, None)
    seen = added = queued = 0
    for a in unsubmitted(brain, region=region, delay=delay):
        seen += 1
        if (a.get("is") or {}).get("sharpe") is not None and a["is"]["sharpe"] < bar.get("min_sharpe", 1.5):
            break                                   # sorted by Sharpe: the rest are weaker
        if added < per_account and strong(a, bar) and save(conn, catalog, account, a):
            added += 1
            if not any_shape:
                continue
        if regular_too and queued < per_regular and strong(a, QUALITY_DEFAULTS) \
                and queue_regular(conn, catalog, account, a, f"scope {scope}" if scope else "brain import", any_shape):
            queued += 1
        if added >= per_account and (not regular_too or queued >= per_regular):
            break
    out = {"status": "done", "seen": seen, "added": added}
    if regular_too:
        out["regular_queued"] = queued
    return out
