"""Daily loop: Power Pool auto-submit after midnight New York, mining the rest of the day.

  python -m ppminer daily              (config: data/autosubmit.json)

Every pass:
  1. for each enabled account with quota left today: run autosubmit once the New York
     day has started (start_after_ny), and again every retry_hours while quota is left
     (the miner keeps producing new picks during the day)
  2. one mining cycle per account (runner.cycle): Lane A Power Pool inventory, Lane C
     regular alphas, explore + revise in every configured region (regular / ATOM there)
Accounts never get each other's candidates (lanes.other_accounts).
"""
from __future__ import annotations

import datetime as dt
import time
from concurrent.futures import ThreadPoolExecutor

from . import autosubmit, lanes
from .catalog import Catalog
from .quota import ny_now
from .fields import sync_step
from .runner import LOGIN_POLL_SECONDS, _brain, cycle, log


def _due(conn, account: str, cfg: dict) -> bool:
    now = ny_now()
    hh, mm = (int(x) for x in cfg.get("start_after_ny", "00:30").split(":"))
    if now.time() < dt.time(hh, mm):
        return False
    row = conn.execute("select value from runner_state where key=?", (f"autosubmit:{account}",)).fetchone()
    if not row:
        return True
    last = dt.datetime.fromisoformat(row[0].strip('"'))
    if last.date() != now.date():
        return True
    return now - last >= dt.timedelta(hours=float(cfg.get("retry_hours", 3)))


def run(db_path: str, catalog_dir: str, hours: float = 24 * 365, batch: int = 30, workers: int = 4) -> None:
    from .db import connect
    conn = connect(db_path)
    lanes.migrate(conn)
    # jobs left 'queued'/'running' by a previous scheduler process can never finish
    # (the process that would record them is gone); free their candidates
    n = conn.execute("delete from resims where status='running' or (status='queued' and coalesce(lane,'')<>'M')").rowcount
    conn.commit()
    t_end = time.time() + hours * 3600
    log(f"daily scheduler start (cleared {n} unfinished jobs from a previous run)")
    while time.time() < t_end:
        cfg = autosubmit.load_config()
        accounts = [a for a, c in cfg.get("accounts", {}).items() if c.get("enabled")]
        regions = cfg.get("regions", ["USA"])
        if not accounts:
            log("no enabled accounts in data/autosubmit.json - sleeping")
            time.sleep(LOGIN_POLL_SECONDS)
            continue
        for account in accounts:
            if _due(conn, account, cfg):
                try:
                    res = autosubmit.run(db_path, catalog_dir, [account])
                    log(f"[{account}] autosubmit: {res.get(account)}")
                    if res.get(account, {}).get("status") != "waiting_login":
                        lanes.set_state(conn, **{f"autosubmit:{account}": ny_now().isoformat()})
                except Exception as e:
                    log(f"[{account}] autosubmit error: {e}")
        for account in accounts:          # field catalogs: a few datasets per region per pass
            b = _brain(account)
            if b is None:
                continue
            for region in regions:
                try:
                    st = sync_step(conn, b, account, region)
                    if st["done"]:
                        log(f"[{account}] fields {region}: +{st['done']} datasets, {st['remaining']} to go")
                except Exception as e:
                    log(f"[{account}] fields {region}: {e}")
        # both accounts mine at the same time, each with its own connection and catalog
        def mine(account):
            c2 = connect(db_path)
            try:
                catalog = Catalog(catalog_dir)
                catalog.add_account_fields(c2, account)
                return cycle(c2, catalog, account, batch=int(cfg.get("batch", batch)), workers=workers,
                             regions=regions, others=accounts,
                             c_share=float(cfg.get("c_share", 0.375)),          # Lane C (funnel) share of each batch
                             slots=int(cfg.get("multisim_slots", 2)))           # multi-simulations (<= 10 each) in flight
            except Exception as e:
                log(f"[{account}] cycle error: {e}")
                return {"status": "error", "sims": 0}
            finally:
                c2.close()

        with ThreadPoolExecutor(len(accounts)) as ex:
            results = list(ex.map(mine, accounts))
        if all(r["status"] in ("waiting_login", "empty") for r in results):
            time.sleep(LOGIN_POLL_SECONDS)
