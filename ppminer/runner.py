"""Continuous miner: Lane A first, Lane C in spare capacity.

  python -m ppminer run --account HT52122 --hours 8 --max-sims 300

Each cycle:
  1. Qwen revises explore ideas that showed some signal (revise.py)
  2. build a batch: Lane A (PP inventory) + a share of Lane C (regular alphas,
     incl. settings sweeps of near misses); Lane C fills whatever Lane A leaves
  3. simulate the batch in parallel on BRAIN
  4. recheck pending checks, rebuild the pick list, write descriptions for new picks
  5. nothing left to simulate -> Qwen explores more fresh datasets
When the session expires it waits for a new login (dashboard / cookie grab /
Telegram) and carries on; unfinished jobs are put back, not lost.
Nothing is ever submitted by the runner.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import cookies as ck
from . import lanes, marketx, revise, templates
from .brain import AuthRequired, Brain, settings_for, summarize
from .catalog import Catalog
from .fields import block
from .pp import submit_expr
from .verdict import regular_verdict, verdict

LOGIN_POLL_SECONDS = 300


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def _brain(account: str) -> Brain | None:
    if account not in ck.saved_accounts():
        return None
    b = Brain.from_cookie(ck.load_cookie(account))
    return b if b.is_authenticated() else None


def _enqueue(conn, account, lane, purpose, cand_id, expr, settings, parent=None) -> int:
    return conn.execute("""insert into resims(candidate_id, account, expr, settings, status, lane, purpose, parent_resim_id)
                           values (?,?,?,?,'queued',?,?,?)""",
                        (cand_id, account, expr, json.dumps(settings), lane, purpose, parent)).lastrowid


def build_batch(conn, account: str, size: int, c_share: float, others: list[str] = (), theme=None) -> list[int]:
    from . import funnel
    # manual jobs from the Search page run first, in the order they were queued
    ids = [r[0] for r in conn.execute("""select id from resims where account=? and status='queued' and lane='M'
                                          order by id limit ?""", (account, size))]
    size -= len(ids)
    if size <= 0:
        return ids
    n_c = max(1, round(size * c_share))
    rows_a = lanes.lane_a(conn, account, size - n_c, others, theme)
    for r in rows_a:
        purpose = {"explore": "explore", "rewrite": "rewrite", "template": "template"}.get(r["status"], "harvest")
        ids.append(_enqueue(conn, account, "A", purpose, r["id"], submit_expr(r["expr"], r["sharpe"]), settings_for(r)))
    room = size - len([r for r in rows_a])
    # Lane C part 1: the tiered funnel fixes the failing check of the best near miss
    for cid, purpose, expr, st, parent in funnel.next_jobs(conn, account, min(3, room)):
        ids.append(_enqueue(conn, account, "C", purpose, cid, expr, st, parent))
        room -= 1
    # Lane C part 2: harvested regular-grade alphas (not already in this batch via Lane A)
    in_batch = {r["id"] for r in rows_a}
    for r in lanes.lane_c(conn, account, max(0, room) + len(in_batch), others):
        if room <= 0:
            break
        if r["id"] in in_batch:
            continue
        ids.append(_enqueue(conn, account, "C", "harvest-regular", r["id"], submit_expr(r["expr"], r["sharpe"]), settings_for(r)))
        room -= 1
    conn.commit()
    return ids


MULTISIM_SIZE = 10          # BRAIN takes up to 10 alphas per multi-simulation


def _chunks(rows: dict, ids: list[int]) -> list[list[int]]:
    """Group jobs into multi-simulations of up to 10 with the same region/universe/delay."""
    groups: dict[tuple, list[int]] = {}
    for i in ids:
        st = json.loads(rows[i]["settings"])
        groups.setdefault((st.get("region"), st.get("universe"), st.get("delay")), []).append(i)
    return [g[k:k + MULTISIM_SIZE] for g in groups.values() for k in range(0, len(g), MULTISIM_SIZE)]


def run_jobs(conn, brain: Brain, account: str, ids: list[int], workers: int, slots: int = 2,
             extra: list[dict] | None = None, mx=None) -> dict:
    """Simulate a batch as BRAIN multi-simulations, `slots` of them in flight (up to 10
    alphas each). DB writes stay on this thread; results are saved as each one lands.
    `extra` (market exploration, saved to markets.db via `mx`) is submitted after the
    batch, so it runs in the first slot that frees up."""
    rows = {i: conn.execute("select * from resims where id=?", (i,)).fetchone() for i in ids}

    def sim_chunk(chunk):
        alphas = brain.multi_simulate([(rows[i]["expr"], json.loads(rows[i]["settings"])) for i in chunk])
        out = []
        for i, a in zip(chunk, alphas):
            if isinstance(a, Exception):
                out.append((i, a))
                continue
            try:
                out.append((i, summarize(a, brain.check(a["id"]))))
            except AuthRequired:
                raise
            except Exception as e:
                out.append((i, e))
        return out

    def sim_extra(jobs):
        alphas = brain.multi_simulate([(j["expr"], j["settings"]) for j in jobs])
        out = []
        for j, a in zip(jobs, alphas):
            try:
                out.append((j, a if isinstance(a, Exception) else summarize(a, brain.check(a["id"]))))
            except AuthRequired:
                raise
            except Exception as e:
                out.append((j, e))
        return out

    stats = {"done": 0, "error": 0, "requeued": 0, "pp_ok": 0, "reg_ok": 0, "market": 0}
    expired = False
    chunks = _chunks(rows, ids)
    with ThreadPoolExecutor(max(1, slots)) as ex:
        futs = {}
        for chunk in chunks:
            for i in chunk:
                conn.execute("update resims set status='running' where id=?", (i,))
            futs[ex.submit(sim_chunk, chunk)] = chunk
        conn.commit()
        extra_fu = ex.submit(sim_extra, extra) if extra and mx is not None else None
        if extra_fu:
            futs[extra_fu] = None
        for fu in as_completed(futs):
            if fu is extra_fu:
                try:
                    for j, res in fu.result():
                        marketx.record(mx, j["id"], res)
                        stats["market"] += not isinstance(res, Exception)
                        if not isinstance(res, Exception):
                            chk = json.dumps(res.get("checks"))
                            log(f"  [X] {j['settings']['region']} {res['alpha_id']} sh={res['sharpe']} to={res['turnover']} "
                                f"PP={verdict(res['sharpe'], chk)[0]} REG={regular_verdict(chk)[0]} | {j['expr'][:70]}")
                except AuthRequired:
                    expired = True
                except Exception as e:
                    for j in extra:
                        marketx.record(mx, j["id"], e)
                    log(f"  [X] market multi-simulation failed: {str(e)[:140]}")
                continue
            chunk = futs[fu]
            try:
                results = fu.result()
            except AuthRequired:
                expired = True
                for i in chunk:
                    conn.execute("delete from resims where id=?", (i,))  # retried after the next login
                stats["requeued"] += len(chunk)
                conn.commit()
                continue
            except Exception as e:                                     # the whole multi-simulation failed
                results = [(i, e) for i in chunk]
            for i, s in results:
                r = rows[i]
                if isinstance(s, Exception):
                    msg = str(s)
                    conn.execute("update resims set status='error', error=? where id=?", (msg[:500], i))
                    if m := re.search(r"Invalid data field (\w+)", msg):
                        block(conn, account, m.group(1), "BRAIN: invalid data field")
                    conn.commit()
                    stats["error"] += 1
                    log(f"  [{r['lane']}] #{r['candidate_id']} error: {re.sub('<[^>]+>', '', msg)[:140]}")
                    continue
                conn.execute("""update resims set alpha_id=?, sharpe=?, fitness=?, turnover=?, returns=?, drawdown=?,
                                checks=?, pp_corr=?, status='done' where id=?""",
                             (s["alpha_id"], s["sharpe"], s["fitness"], s["turnover"], s["returns"], s["drawdown"],
                              json.dumps(s.get("checks")), s.get("pp_corr"), i))
                conn.commit()            # save each result as it lands; an interrupted batch keeps its finished sims
                chk = json.dumps(s.get("checks"))
                pv, rv = verdict(s["sharpe"], chk)[0], regular_verdict(chk)[0]
                stats["done"] += 1
                stats["pp_ok"] += pv == "pp_ok"
                stats["reg_ok"] += rv == "reg_ok"
                log(f"  [{r['lane']}] #{r['candidate_id']} {s['alpha_id']} sh={s['sharpe']} to={s['turnover']} "
                    f"PP={pv} REG={rv} ({r['purpose']})")
    conn.commit()
    stats["expired"] = expired
    return stats


def do_revisions(conn, catalog, account: str, limit: int, workers: int = 4, theme=None) -> int:
    rows = revise.todo(conn, account, theme)[:limit]
    added = 0
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(revise.revise, _ro(conn), catalog, account, r, theme): r for r in rows}
        for fu in as_completed(futs):
            r = futs[fu]
            try:
                revs = fu.result()
            except Exception as e:
                revise.mark(conn, r["id"], False, f"error: {e}")
                continue
            n = revise.save(conn, catalog, r, revs)
            revise.mark(conn, r["id"], n > 0, f"{n} revisions kept of {len(revs)}" if revs else "dropped: no signal")
            added += n
    return added


def _ro(conn):
    path = conn.execute("pragma database_list").fetchone()[2]
    # handed to a worker thread (one connection per job), so not tied to the creating thread
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def after_batch(conn, catalog, brain: Brain, account: str) -> int:
    """Recheck pending PP checks, rebuild picks, describe new picks. Returns number of picks."""
    from .describe import describe, save as save_desc
    from .picks import build, fetch_pnl, passing
    from . import ppcorr
    from .verdict import quality_gate
    fallbacks = 0
    for r in conn.execute("select * from resims where account=? and status='done' and lane is not null", (account,)).fetchall():
        if verdict(r["sharpe"], r["checks"])[0] == "pending":
            s = summarize(brain.alpha(r["alpha_id"]), brain.check(r["alpha_id"]))
            checks = s.get("checks") or []
            # BRAIN often never fills POWER_POOL_CORRELATION in /check; for alphas strong enough
            # to submit, estimate it from correlations/self (limited per cycle: one call each)
            if (ppcorr.needs_fallback(checks) and fallbacks < 12
                    and not quality_gate(r["sharpe"], r["fitness"], r["turnover"], json.dumps(checks))):
                fallbacks += 1
                try:
                    checks = ppcorr.fill(brain, conn, r["alpha_id"], checks, account)
                except Exception as e:
                    log(f"  correlations/self for {r['alpha_id']} failed: {e}")
            conn.execute("update resims set checks=? where id=?", (json.dumps(checks), r["id"]))
    conn.commit()
    if fallbacks:
        log(f"[{account}] Power Pool correlation estimated from correlations/self for {fallbacks} alphas")
    rows = passing(conn, account)
    fetch_pnl(conn, brain, account, [r["alpha_id"] for r in rows])
    picks = build(conn, account)
    for p in picks:
        cid = p["row"]["candidate_id"]
        if conn.execute("select 1 from descriptions where candidate_id=?", (cid,)).fetchone():
            continue
        try:
            save_desc(conn, cid, describe(conn.execute("select * from candidates where id=?", (cid,)).fetchone(), catalog))
        except Exception as e:
            log(f"  describe #{cid} failed: {e}")
    return len(picks)


def explore_regions(conn, catalog, account: str, regions: list[str], per_region: int = 3, min_queue: int = 10) -> int:
    """Keep a small stock of unsimulated explore ideas in every region the account has synced."""
    from .explore import explore, fresh_datasets, save as save_explore
    added = 0
    for region in regions:
        queued = conn.execute("""select count(*) from candidates c where c.status='explore' and c.region=?
                                 and not exists (select 1 from resims r where r.candidate_id=c.id and r.account=?)""",
                              (region, account)).fetchone()[0]
        if queued >= min_queue:
            continue
        for ds in fresh_datasets(conn, account, region)[:per_region]:
            try:
                added += save_explore(conn, ds, explore(_ro(conn), catalog, account, ds, region), region)
            except Exception as e:
                log(f"  explore {region}/{ds['dataset']} failed: {e}")
    return added


def theme_ideas(conn, catalog, account: str, theme, others: list[str] = (), min_datasets: int = 6,
                per_pass: int = 3) -> int:
    """Keep untested template alphas (Search 1) on enough datasets of the theme's region(s)
    for Lane A's theme share: the harvested inventory barely covers regions like GLB.
    The shortlist takes at most 2 untested ideas per dataset, hence the dataset count."""
    from .explore import fresh_datasets
    added = 0
    for region in sorted(theme.regions):
        taken = lanes.theme_datasets(conn, theme, [o for o in others if o != account])   # the other account's
        open_ds = {r[0] for r in conn.execute("""select distinct c.datasets from candidates c where c.status in ('template', 'explore')
                                  and c.region=? and not exists (select 1 from resims r where r.candidate_id=c.id and r.account=?)""",
                                              (region, account))} - taken
        if len(open_ds) >= min_datasets:
            continue
        for ds in fresh_datasets(conn, account, region, theme.delay or 1, theme.categories or None)[:per_pass]:
            rows = templates.generate(conn, catalog, account, region, "s1", ds["dataset"], 3, order="fresh") \
                or templates.generate(conn, catalog, account, region, "s1", ds["dataset"], 3, order="coverage")
            added += templates.save(conn, region, rows, theme.delay or 1, source=f"theme:{theme.name}")
    return added


def cycle(conn, catalog, account: str, *, batch: int = 8, workers: int = 4, c_share: float = 0.375, slots: int = 2,
          regions: list[str] = ("USA",), others: list[str] = (), sims_left: int = 10**9,
          market_regions: list[str] | None = None, theme=None) -> dict:
    """One round for one account: revise, top up explore ideas, simulate a batch, refresh picks."""
    brain = _brain(account)
    if brain is None:
        lanes.set_state(conn, status="waiting_login", account=account)
        return {"status": "waiting_login", "sims": 0}
    added = do_revisions(conn, catalog, account, limit=8, theme=theme)
    if added:
        log(f"[{account}] Qwen revisions: {added} new explore variants")
    n = explore_regions(conn, catalog, account, list(regions))
    if n:
        log(f"[{account}] Qwen explore: {n} new ideas across {', '.join(regions)}")
    if theme is not None and theme.regions:
        n = theme_ideas(conn, catalog, account, theme, others)
        if n:
            log(f"[{account}] theme {theme.name}: {n} new template alphas")
    ids = build_batch(conn, account, min(batch, sims_left), c_share, others, theme)
    if not ids:
        return {"status": "empty", "sims": 0}
    lanes.set_state(conn, status="simulating", account=account, batch=len(ids))
    log(f"[{account}] batch of {len(ids)}: " + ", ".join(f"{k}={v}" for k, v in _lane_counts(conn, ids).items()))
    extra, mx = [], None
    if market_regions:          # template alphas in other markets, in the slot the batch leaves free
        mx = marketx.connect()
        try:
            extra = marketx.next_chunk(conn, mx, account, market_regions)
        except Exception as e:
            log(f"[{account}] market exploration: {e}")
        if extra:
            log(f"[{account}] market exploration: {len(extra)} template alphas in {extra[0]['settings']['region']}")
    st = run_jobs(conn, brain, account, ids, workers, slots, extra, mx)
    if st["expired"]:
        return {"status": "waiting_login", "sims": st["done"] + st["error"]}
    n_picks = after_batch(conn, catalog, brain, account)
    lanes.set_state(conn, status="idle", account=account, last_batch=st, picks=n_picks)
    log(f"[{account}] batch done: {st} | picks {n_picks}")
    return {"status": "ok", "sims": st["done"] + st["error"]}


def run(db_path: str, catalog_dir: str, account: str, hours: float, max_sims: int, batch: int = 8,
        workers: int = 4, c_share: float = 0.25, regions: list[str] = ("USA",), others: list[str] = ()) -> None:
    from .db import connect
    conn = connect(db_path)
    lanes.migrate(conn)
    catalog = Catalog(catalog_dir)
    catalog.add_account_fields(conn, account)
    conn.execute("delete from resims where account=? and (status='running' or (status='queued' and coalesce(lane,'')<>'M'))",
                 (account,))  # stale from a crash; queued manual jobs are kept
    conn.commit()
    t_end, sims, synced = time.time() + hours * 3600, 0, False
    log(f"runner start: account {account}, {hours}h, max {max_sims} sims, batch {batch}, Lane C share {c_share}, "
        f"regions {', '.join(regions)}")
    while time.time() < t_end and sims < max_sims:
        if not synced and (b := _brain(account)):
            log(f"synced {lanes.sync_submitted(conn, b, account, catalog)} submitted alphas of {account}")
            synced = True
        r = cycle(conn, catalog, account, batch=batch, workers=workers, c_share=c_share, regions=regions,
                  others=others, sims_left=max_sims - sims)
        sims += r["sims"]
        if r["status"] == "waiting_login":
            log(f"session for {account} not live - waiting for a new login (checking every {LOGIN_POLL_SECONDS // 60} min)")
            time.sleep(LOGIN_POLL_SECONDS)
        elif r["status"] == "empty":
            log("nothing left to simulate or explore - stopping")
            break
    lanes.set_state(conn, status="stopped", account=account, sims=sims)
    log(f"runner stopped after {sims} simulations")


def _lane_counts(conn, ids):
    out = {}
    for i in ids:
        lane = conn.execute("select lane from resims where id=?", (i,)).fetchone()[0]
        out[f"lane {lane}"] = out.get(f"lane {lane}", 0) + 1
    return out
