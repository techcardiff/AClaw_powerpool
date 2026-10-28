"""Daily loop: Power Pool auto-submit after midnight New York, mining the rest of the day.

  python -m ppminer daily              (config: data/autosubmit.json)

Every pass:
  1. for each enabled account with quota left today: run autosubmit once the New York
     day has started (start_after_ny), and again every retry_hours while quota is left
     (the miner keeps producing new picks during the day). It runs in a background
     thread with its own database connection, so mining never waits for its BRAIN
     checks; a new run starts only after the previous one finished
  2. one mining cycle per account (runner.cycle): Lane A Power Pool inventory, Lane C
     regular alphas, explore + revise in every configured region (regular / ATOM there)
Lane A is steered to the Power Pool theme BRAIN lists right now (theme.detect): half of
it goes to matching candidates, and the theme's region joins the field sync and explore
regions even when data/autosubmit.json doesn't list it.
Accounts never get each other's candidates (lanes.other_accounts).
"""
from __future__ import annotations

import datetime as dt
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import autosubmit, lanes, marketx
from . import theme as th
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


def _autosubmit(db_path: str, catalog_dir: str, accounts: list[str]) -> None:
    """One auto-submit pass for the due accounts, off the mining thread."""
    from .db import connect
    conn = connect(db_path)
    try:
        for account in accounts:
            try:
                res = autosubmit.run(db_path, catalog_dir, [account])
                log(f"[{account}] autosubmit: {res.get(account)}")
                if res.get(account, {}).get("status") != "waiting_login":
                    lanes.set_state(conn, **{f"autosubmit:{account}": ny_now().isoformat()})
            except Exception as e:
                log(f"[{account}] autosubmit error: {e}")
    finally:
        conn.close()


def run(db_path: str, catalog_dir: str, hours: float = 24 * 365, batch: int = 30, workers: int = 4) -> None:
    from .db import connect
    conn = connect(db_path)
    lanes.migrate(conn)
    # jobs left 'queued'/'running' by a previous scheduler process can never finish
    # (the process that would record them is gone); free their candidates
    n = conn.execute("delete from resims where status='running' or (status='queued' and coalesce(lane,'')<>'M')").rowcount
    conn.commit()
    t_end = time.time() + hours * 3600
    n += marketx.clear_unfinished(marketx.connect())
    log(f"daily scheduler start (cleared {n} unfinished jobs from a previous run)")
    last_theme = None
    submitter: threading.Thread | None = None
    while time.time() < t_end:
        cfg = autosubmit.load_config()
        accounts = [a for a, c in cfg.get("accounts", {}).items() if c.get("enabled")]
        regions = cfg.get("regions", ["USA"])
        try:
            theme = th.current(conn)
        except Exception as e:
            theme = None
            log(f"theme detection: {e}")
        if theme is not None:
            regions = regions + sorted(r for r in theme.regions if r not in regions)
        if (theme and theme.name) != last_theme:
            last_theme = theme and theme.name
            log(f"Lane A theme: {theme.name} ({theme.raw})" if theme else "Lane A theme: none detected")
        if not accounts:
            log("no enabled accounts in data/autosubmit.json - sleeping")
            time.sleep(LOGIN_POLL_SECONDS)
            continue
        due = [a for a in accounts if _due(conn, a, cfg)]
        if due and (submitter is None or not submitter.is_alive()):
            submitter = threading.Thread(target=_autosubmit, args=(db_path, catalog_dir, due), daemon=True,
                                         name="autosubmit")
            submitter.start()
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
        mxc = cfg.get("market_explore", {"enabled": True, "regions": ["EUR", "ASI", "GLB"]})
        mx_regions = mxc.get("regions") if mxc.get("enabled", True) else None
        if mx_regions and theme is not None:   # the theme region is Lane A's now, not exploration's
            mx_regions = [r for r in mx_regions if r not in theme.regions] or None
        # both accounts mine at the same time, each with its own connection and catalog
        def mine(account):
            c2 = connect(db_path)
            try:
                catalog = Catalog(catalog_dir)
                catalog.add_account_fields(c2, account)
                return cycle(c2, catalog, account, batch=int(cfg.get("batch", batch)), workers=workers,
                             regions=regions, others=accounts,
                             c_share=float(cfg.get("c_share", 0.375)),          # Lane C (funnel) share of each batch
                             slots=int(cfg.get("multisim_slots", 2)),           # multi-simulations (<= 10 each) in flight
                             market_regions=mx_regions,                         # template exploration in free slots
                             theme=theme)                                       # Lane A theme share
            except Exception as e:
                log(f"[{account}] cycle error: {e}")
                return {"status": "error", "sims": 0}
            finally:
                c2.close()

        with ThreadPoolExecutor(len(accounts)) as ex:
            results = list(ex.map(mine, accounts))
        if all(r["status"] in ("waiting_login", "empty", "paused") for r in results):
            time.sleep(LOGIN_POLL_SECONDS)
