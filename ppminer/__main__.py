"""Power Pool miner CLI.

  python -m ppminer harvest            merge past simulations into data/pp.db
  python -m ppminer stats              counts by status / category
  python -m ppminer shortlist -n 40    diverse list for the active theme -> data/reports/
  python -m ppminer describe --top 20  Qwen3.8 descriptions for the shortlist
  python -m ppminer purify --limit 50  Qwen3.8 single-category rewrites
  python -m ppminer resim --cookie-file F --top 10   re-simulate shortlist on BRAIN
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import db as dbm
from .catalog import Catalog
from .harvest import agent_rows, harvest, studio_rows
from .theme import active_themes, load_themes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.getenv("PP_DB", os.path.join(ROOT, "data", "pp.db"))
CATALOG_DIR = os.getenv("PP_CATALOG", os.path.join(ROOT, "data", "catalog"))
STUDIO_DB = os.getenv("PP_STUDIO_DB", "/Users/macbook/Documents/Work/quant/alpha_results.db")
AGENT_EXPORT = os.getenv("PP_AGENT_EXPORT", os.path.join(ROOT, "data", "sources", "pp_export.sqlite"))
THEMES = os.path.join(ROOT, "themes.json")
REPORTS = os.path.join(ROOT, "data", "reports")


def _theme(args):
    themes = load_themes(THEMES)
    day = dt.date.fromisoformat(args.date) if getattr(args, "date", None) else dt.date.today()
    act = active_themes(themes, day)
    if not act:
        sys.exit(f"no active theme on {day}; add it to themes.json")
    return act[0]


def cmd_harvest(args):
    conn, cat = dbm.connect(DB_PATH), Catalog(CATALOG_DIR)
    sources = []
    if os.path.exists(STUDIO_DB):
        sources.append(studio_rows(STUDIO_DB))
    if os.path.exists(AGENT_EXPORT):
        sources.append(agent_rows(AGENT_EXPORT))
    print(harvest(conn, cat, sources))
    cmd_stats(args)


def cmd_stats(args):
    conn = dbm.connect(DB_PATH)
    print("by status:", dict(conn.execute("select status, count(*) from candidates group by 1").fetchall()))
    c = Counter()
    for row in conn.execute("select categories from candidates where status='pp_ready' and family_rank=1"):
        c[",".join(json.loads(row[0]))] += 1
    print("pp_ready family representatives by category:", dict(c.most_common()))
    print("descriptions:", conn.execute("select count(*) from descriptions").fetchone()[0],
          "| rewrites valid/total:", conn.execute("select sum(valid), count(*) from rewrites").fetchone()[:],
          "| resims:", conn.execute("select count(*) from resims").fetchone()[0])


LANE_A_STATUSES = "pp_ready,pp_multi,rewrite,explore"


def _shortlist(args, conn):
    from .select import shortlist
    from .theme import Theme
    lane_a = getattr(args, "lane", "theme") == "A"
    theme = None if lane_a else _theme(args)
    statuses = args.status or (LANE_A_STATUSES if lane_a else "pp_ready")
    account = getattr(args, "account", None)
    skip_ids, stems = (_other_accounts_used(conn, account) if account else (set(), set()))
    rows = shortlist(conn, theme, n=args.n, statuses=tuple(statuses.split(",")),
                     account=account, skip_done=getattr(args, "skip_done", False),
                     skip_ids=skip_ids, exclude_stems=stems or None)
    label = theme or Theme("Lane A (no theme)", dt.date.today(), dt.date.today(), raw="any category / region")
    return label, rows


def _other_accounts_used(conn, account):
    """Alphas already simulated on another account, and their field families: each
    account researches its own ideas (no copies of the same alpha across accounts)."""
    from .select import _stems
    ids, stems = set(), set()
    for cid, fields in conn.execute("""select r.candidate_id, c.fields from resims r join candidates c
                                       on c.id = r.candidate_id where r.account != ?""", (account,)):
        ids.add(cid)
        stems.add(_stems(json.loads(fields)))
    return ids, stems


def cmd_shortlist(args):
    conn = dbm.connect(DB_PATH)
    theme, rows = _shortlist(args, conn)
    os.makedirs(REPORTS, exist_ok=True)
    path = os.path.join(REPORTS, f"shortlist_{'laneA_' if args.lane == 'A' else ''}{dt.date.today()}.md")
    with open(path, "w") as f:
        f.write(f"# Shortlist for {theme.name}\n\nRule: `{theme.raw}`\n\n")
        f.write("| # | id | region | cat | datasets | ops | sharpe | fit | TO | flags | expression |\n")
        f.write("|---|---|---|---|---|---|---|---|---|---|---|\n")
        for i, r in enumerate(rows, 1):
            f.write(f"| {i} | {r['id']} | {r['region']} | {','.join(json.loads(r['categories']))} | "
                    f"{','.join(json.loads(r['datasets']))} | {r['n_ops']} | {r['sharpe'] or 0:.2f} | "
                    f"{r['fitness'] or 0:.2f} | {r['turnover'] or 0:.2f} | {','.join(json.loads(r['flags']))} | "
                    f"`{r['expr'].replace('|', '/')}` |\n")
    for i, r in enumerate(rows, 1):
        print(f"{i:3d} #{r['id']:<7d} {','.join(json.loads(r['datasets'])):22s} ops={r['n_ops']} "
              f"sh={r['sharpe'] or 0:5.2f} to={r['turnover'] or 0:.2f}  {r['expr'][:90]}")
    print(f"\n{len(rows)} picked for {theme.name} -> {path}")


def _parallel(fn, items, workers):
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(fn, it): it for it in items}
        for fu in as_completed(futs):
            yield futs[fu], fu


def cmd_describe(args):
    from .describe import describe, save
    conn, cat = dbm.connect(DB_PATH), Catalog(CATALOG_DIR)
    _, rows = _shortlist(args, conn)
    rows = [r for r in rows[: args.top]
            if not conn.execute("select 1 from descriptions where candidate_id=?", (r["id"],)).fetchone()]
    ok = 0
    for row, fu in _parallel(lambda r: describe(r, cat), rows, args.workers):
        try:
            d = fu.result()
            save(conn, row["id"], d)
            ok += 1
            print(f"#{row['id']} ({len(d['text'])} chars)\n{d['text']}\n")
        except Exception as e:  # keep going; record failure
            conn.execute("insert into llm_jobs(candidate_id, kind, ok, error) values (?,?,0,?)",
                         (row["id"], "describe", str(e)[:500]))
            conn.commit()
            print(f"#{row['id']} failed: {e}")
    print(f"described {ok}/{len(rows)}")


def cmd_purify(args):
    from .purify import purify, save, target_categories
    conn, cat = dbm.connect(DB_PATH), Catalog(CATALOG_DIR)
    from .theme import Theme
    # Lane A: any category, keep the category of the strongest field family
    theme = Theme("Lane A", dt.date.today(), dt.date.today()) if args.lane == "A" else _theme(args)
    from .select import _stems
    def family_key(fields, region, target):
        # only the fields that survive the rewrite define what signal it really is
        return target, _stems([f for f in fields if (i := cat.get(f, region)) and i.category == target])

    done_stems = {family_key(json.loads(r[0]), r[1], r[2]) for r in conn.execute(
        "select c.fields, c.region, w.target_category from rewrites w join candidates c on c.id = w.parent_id")}
    rows = conn.execute("""select * from candidates c where status='purify' and family_rank=1
                             and not exists (select 1 from rewrites w where w.parent_id=c.id)
                             and coalesce(json_extract(flags, '$'), '[]') not like '%suspicious%'
                           order by abs(sharpe) desc limit ?""", (args.limit * 50,)).fetchall()
    jobs = []
    for r in rows:
        if theme.delay is not None and r["delay"] != theme.delay:
            continue
        targets = target_categories(r, cat, theme.categories or None)[:1]
        if not targets:
            continue
        key = family_key(json.loads(r["fields"]), r["region"], targets[0])
        if key in done_stems:            # one parent per signal family, so rewrites stay diverse
            continue
        done_stems.add(key)
        jobs.append((r, targets[0]))
        if len(jobs) >= args.limit:
            break
    added = valid = total = 0
    for (row, target), fu in _parallel(lambda j: purify(j[0], j[1], cat), jobs, args.workers):
        try:
            res = fu.result()
        except Exception as e:
            print(f"#{row['id']} failed: {e}")
            continue
        total += len(res)
        valid += sum(r["valid"] for r in res)
        added += save(conn, row, target, res)
        for r in res:
            print(f"#{row['id']} -> {target} {'OK ' if r['valid'] else 'BAD'} {r['expr'][:100]} {r['problems'] or ''}")
    print(f"\nparents {len(jobs)} | variants {total} | valid {valid} | new candidates {added}")


def _hours(sec):
    return f"{sec / 3600:.1f}h left" if sec else "expiry unknown"


def cmd_cookie(args):
    from . import cookies as ck
    if args.action == "grab":
        sessions = ck.chrome_sessions()
        if not sessions:
            sys.exit("no live BRAIN session in any Chrome profile; log in at platform.worldquantbrain.com first")
        for s in sessions:
            name = ck.alias_for(s["user_id"])
            ck.save_cookie(name, s["cookie"])
            print(f"saved {name:10s} (user {s['user_id']}, Chrome '{s['profile']}', {_hours(s['expiry_seconds'])})")
    elif args.action == "set":
        import getpass
        if not args.account:
            sys.exit("cookie set needs --account")
        cookie = getpass.getpass("paste BRAIN cookie (t=...), input hidden: ")
        who = ck.whoami(cookie)
        if not who:
            sys.exit("that cookie is not a live BRAIN session")
        ck.save_cookie(args.account, ck.only_session(cookie))
        print(f"saved {args.account} (user {who['user_id']}, {_hours(who['expiry_seconds'])})")
    elif args.action == "import-old":
        import glob
        for path in sorted(glob.glob(os.path.join(os.path.dirname(STUDIO_DB), "brain_cookies*.txt"))):
            with open(path) as f:
                who = ck.whoami(f.read())
            if not who:
                print(f"{os.path.basename(path)}: expired")
                continue
            name = ck.alias_for(who["user_id"])
            with open(path) as f:
                ck.save_cookie(name, ck.only_session(f.read()))
            print(f"{os.path.basename(path)}: saved as {name} (user {who['user_id']}, {_hours(who['expiry_seconds'])})")


def cmd_telegram(args):
    from .telegram import Broker, ConfigError, load_config
    try:
        cfg = load_config()
    except ConfigError as e:
        sys.exit(str(e))
    broker = Broker(cfg)
    if args.action == "check":
        me = broker.tg.call("getMe")
        print(f"bot @{me['username']} ok")
        for name, a in cfg["accounts"].items():
            try:
                broker.tg.send(a["chat_id"], f"👋 ppminer test: login links for {name} will arrive here.")
                print(f"test message sent for {name} (chat {a['chat_id']})")
            except RuntimeError as e:
                print(f"{name}: could not message chat {a['chat_id']} ({e}). Check the chat id, "
                      f"and that the owner opened https://t.me/{me['username']} and tapped Start.")
    elif args.action == "login":
        names = [args.account] if args.account else list(cfg["accounts"])
        for n in names:
            if n not in cfg["accounts"]:
                sys.exit(f"{n} not in telegram.json")
            print(f"{n}: link sent to Telegram, waiting for the face scan…")
            print(f"{n}: {'logged in' if broker.login(n, 'requested') else 'not logged in'}")
    else:
        broker.run()


def cmd_login_status(args):
    from . import cookies as ck
    names = ck.saved_accounts()
    if not names:
        print("no saved sessions; run: python -m ppminer cookie grab")
    for name in names:
        who = ck.whoami(ck.load_cookie(name))
        print(f"{name:10s} " + (f"LIVE  user {who['user_id']}, {_hours(who['expiry_seconds'])}" if who
                                 else "EXPIRED  log in again in Chrome, then: python -m ppminer cookie grab"))


def cmd_resim(args):
    from . import cookies as ck
    from .brain import Brain, settings_for, summarize
    from .pp import submit_expr
    conn = dbm.connect(DB_PATH)
    if args.account not in ck.saved_accounts():
        sys.exit(f"no saved session '{args.account}' (have: {', '.join(ck.saved_accounts()) or 'none'}); "
                 "log in in Chrome, then: python -m ppminer cookie grab")
    brain = Brain.from_cookie(ck.load_cookie(args.account))
    if not brain.is_authenticated():
        sys.exit(f"session for {args.account} expired; log in in Chrome, then: python -m ppminer cookie grab")
    import re
    from .fields import block
    from .verdict import verdict
    args.skip_done = True
    _, rows = _shortlist(args, conn)
    rows = rows[: args.top]
    if not rows:
        sys.exit("nothing left to simulate for this selection (synced fields? run: python -m ppminer fields-sync)")
    jobs = []
    for row in rows:          # DB writes stay on this thread; workers only talk to BRAIN
        expr = submit_expr(row["expr"], row["sharpe"])
        st = settings_for(row)
        rid = conn.execute("insert into resims(candidate_id, account, expr, settings, status) values (?,?,?,?,'queued')",
                           (row["id"], args.account, expr, json.dumps(st))).lastrowid
        jobs.append((rid, row, expr, st))
    conn.commit()

    def run(job):
        rid, row, expr, st = job
        a = brain.simulate(expr, st)
        return summarize(a, brain.check(a["id"]))

    for (rid, row, expr, st), fu in _parallel(run, jobs, args.sim_workers):
        try:
            s = fu.result()
            conn.execute("""update resims set alpha_id=?, sharpe=?, fitness=?, turnover=?, returns=?, drawdown=?,
                            checks=?, pp_corr=?, status='done' where id=?""",
                         (s["alpha_id"], s["sharpe"], s["fitness"], s["turnover"], s["returns"], s["drawdown"],
                          json.dumps(s.get("checks")), s.get("pp_corr"), rid))
            v, why = verdict(s["sharpe"], json.dumps(s.get("checks")))
            print(f"#{row['id']} {s['alpha_id']} sh={s['sharpe']} to={s['turnover']} -> {v} {', '.join(why)}")
        except Exception as e:
            msg = str(e)
            conn.execute("update resims set status='error', error=? where id=?", (msg[:500], rid))
            if m := re.search(r"Invalid data field (\w+)", msg):
                block(conn, args.account, m.group(1), "BRAIN: invalid data field")
            print(f"#{row['id']} error: {re.sub('<[^>]+>', '', msg)[:200]}")
        conn.commit()


def cmd_picklist(args):
    from . import cookies as ck
    from .brain import Brain
    from .describe import describe, save
    from .picks import build, fetch_pnl, passing
    conn, cat = dbm.connect(DB_PATH), Catalog(CATALOG_DIR)
    cat.add_account_fields(conn, args.account)
    rows = passing(conn, args.account)
    print(f"{len(rows)} re-simulated alphas meet the Power Pool bar on {args.account}")
    brain = Brain.from_cookie(ck.load_cookie(args.account))
    if not brain.is_authenticated():
        sys.exit(f"session for {args.account} expired (needed to fetch PnL)")
    print("fetched PnL for", fetch_pnl(conn, brain, args.account, [r["alpha_id"] for r in rows]), "alphas")
    picks = build(conn, args.account, args.threshold)
    for p in picks:
        r = p["row"]
        folded = ", ".join(f"{a} ({c})" for a, c in p["members"]) or "-"
        print(f"PICK {p['alpha_id']} #{r['candidate_id']} sh={r['sharpe']} to={r['turnover']} "
              f"max_corr={p['max_corr']:.2f} | folded: {folded}\n     {r['expr'][:120]}")
    print(f"\n{len(rows)} passing -> {len(picks)} picks (corr threshold {args.threshold})")
    if args.describe:
        need = [p["row"]["candidate_id"] for p in picks
                if not conn.execute("select 1 from descriptions where candidate_id=?", (p["row"]["candidate_id"],)).fetchone()]
        cands = [conn.execute("select * from candidates where id=?", (cid,)).fetchone() for cid in need]
        for row, fu in _parallel(lambda r: describe(r, cat), cands, 8):
            try:
                save(conn, row["id"], fu.result())
            except Exception as e:
                print(f"describe #{row['id']} failed: {e}")
        print(f"descriptions written for {len(need)} picks")


def cmd_explore(args):
    from .explore import explore, fresh_datasets, save
    conn, cat = dbm.connect(DB_PATH), Catalog(CATALOG_DIR)
    cat.add_account_fields(conn, args.account)
    cats = set(args.categories.split(",")) if args.categories else None
    todo = fresh_datasets(conn, args.account, args.region, args.delay, cats)[: args.limit]
    print(f"{len(todo)} fresh datasets to explore")
    added = valid = total = 0
    for ds, fu in _parallel(lambda d: explore(conn_ro(), cat, args.account, d, args.region, args.delay), todo, args.workers):
        try:
            res = fu.result()
        except Exception as e:
            print(f"{ds['dataset']} failed: {e}")
            continue
        total += len(res)
        valid += sum(r["valid"] for r in res)
        added += save(conn, ds, res, args.region, args.delay)
        for r in res:
            print(f"{ds['dataset']:22s} {'OK ' if r['valid'] else 'BAD'} {r['expr'][:100]} {r['problems'] or ''}")
    print(f"\ndatasets {len(todo)} | ideas {total} | valid {valid} | new candidates {added}")


def conn_ro():
    """Per-thread read-only connection (sqlite connections can't cross threads)."""
    import sqlite3
    c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def cmd_fields_sync(args):
    from . import cookies as ck
    from .brain import Brain
    from .fields import sync
    brain = Brain.from_cookie(ck.load_cookie(args.account))
    if not brain.is_authenticated():
        sys.exit(f"session for {args.account} expired")
    print(sync(dbm.connect(DB_PATH), brain, args.account, args.region, args.delay, args.universe))


def cmd_lanes(args):
    from . import lanes
    from .verdict import regular_verdict
    conn = dbm.connect(DB_PATH)
    lanes.migrate(conn)
    pp, own = lanes.collide_stems(conn, args.account)
    print(f"skipping {len(pp)} families that collide with the Power Pool, {len(own)} of the account's own alphas")
    for name, rows in (("Lane A", lanes.lane_a(conn, args.account, args.n)), ("Lane C", lanes.lane_c(conn, args.account, args.n))):
        print(f"\n{name}: {len(rows)}")
        for r in rows:
            print(f"  #{r['id']:<6} {r['status']:9s} {','.join(json.loads(r['datasets'])):22s} sh={r['sharpe'] or 0:5.2f} "
                  f"fit={r['fitness'] or 0:5.2f} ops={r['n_ops']}  {r['expr'][:80]}")
    for p in lanes.near_misses(conn, args.account):
        _, fails = regular_verdict(p["checks"])
        print(f"\nLane C tune {p['alpha_id']} fails {fails}:")
        for why, _ in lanes.tune_variants(json.loads(p["settings"]), fails):
            print("   ", why)


def cmd_recheck(args):
    """Re-run BRAIN's /check for finished re-sims whose Power Pool checks were not computed yet."""
    from . import cookies as ck
    from .brain import Brain, summarize
    from .verdict import verdict
    conn = dbm.connect(DB_PATH)
    brain = Brain.from_cookie(ck.load_cookie(args.account))
    if not brain.is_authenticated():
        sys.exit(f"session for {args.account} expired; log in in Chrome, then: python -m ppminer cookie grab")
    rows = conn.execute("select * from resims where status='done' and account=?", (args.account,)).fetchall()
    for r in rows:
        if verdict(r["sharpe"], r["checks"])[0] != "pending" and not args.all:
            continue
        s = summarize(brain.alpha(r["alpha_id"]), brain.check(r["alpha_id"]))
        conn.execute("update resims set checks=?, pp_corr=? where id=?", (json.dumps(s.get("checks")), s.get("pp_corr"), r["id"]))
        conn.commit()
        v, why = verdict(r["sharpe"], json.dumps(s.get("checks")))
        print(f"#{r['candidate_id']} {r['alpha_id']} -> {v} {', '.join(why)}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="ppminer")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("harvest").set_defaults(fn=cmd_harvest)
    sub.add_parser("stats").set_defaults(fn=cmd_stats)
    for name, fn in (("shortlist", cmd_shortlist), ("describe", cmd_describe), ("resim", cmd_resim)):
        sp = sub.add_parser(name)
        sp.add_argument("-n", type=int, default=40)
        sp.add_argument("--date")
        sp.add_argument("--status", default=None, help=f"default: pp_ready (theme) / {LANE_A_STATUSES} (lane A)")
        sp.add_argument("--lane", choices=["theme", "A"], default="theme",
                        help="theme = active Power Pool theme; A = general inventory, any category")
        sp.add_argument("--top", type=int, default=20)
        sp.add_argument("--workers", type=int, default=4)
        sp.set_defaults(fn=fn)
        if name == "resim":
            sp.add_argument("--account", required=True, help="name of a saved session (see login-status)")
            sp.add_argument("--sim-workers", type=int, default=3, help="simulations in flight at once")
        else:
            sp.add_argument("--account", help="only alphas this account's data access allows")
    sp = sub.add_parser("cookie")
    sp.add_argument("action", choices=["grab", "set", "import-old"])
    sp.add_argument("--account")
    sp.set_defaults(fn=cmd_cookie)
    sub.add_parser("login-status").set_defaults(fn=cmd_login_status)
    sp = sub.add_parser("telegram", help="face-scan login links via Telegram")
    sp.add_argument("action", choices=["run", "login", "check"],
                    help="run = bot + automatic renewals; login = one login now; check = test config + send a test message")
    sp.add_argument("--account")
    sp.set_defaults(fn=cmd_telegram)
    sp = sub.add_parser("picklist", help="cluster passing alphas by PnL correlation, one pick per cluster")
    sp.add_argument("--account", required=True)
    sp.add_argument("--threshold", type=float, default=0.5)
    sp.add_argument("--no-describe", dest="describe", action="store_false")
    sp.set_defaults(fn=cmd_picklist)
    sp = sub.add_parser("explore", help="Lane A: Qwen alphas on datasets we have never mined")
    sp.add_argument("--account", required=True)
    sp.add_argument("--region", default="USA")
    sp.add_argument("--delay", type=int, default=1)
    sp.add_argument("--categories", help="comma list, e.g. news,analyst (default: all)")
    sp.add_argument("--limit", type=int, default=20, help="datasets per run")
    sp.add_argument("--workers", type=int, default=8)
    sp.set_defaults(fn=cmd_explore)
    sp = sub.add_parser("fields-sync", help="fetch the datasets/fields an account can use")
    sp.add_argument("--account", required=True)
    sp.add_argument("--region", default="USA")
    sp.add_argument("--delay", type=int, default=1)
    sp.add_argument("--universe", default="TOP3000")
    sp.set_defaults(fn=cmd_fields_sync)
    sp = sub.add_parser("recheck")
    sp.add_argument("--account", required=True)
    sp.add_argument("--all", action="store_true", help="recheck every finished re-sim, not only pending ones")
    sp.set_defaults(fn=cmd_recheck)
    sp = sub.add_parser("web")
    sp.add_argument("--port", type=int, default=5070)
    sp.set_defaults(fn=lambda a: __import__("ppminer.web", fromlist=["serve"]).serve(DB_PATH, a.port))
    sp = sub.add_parser("purify")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--workers", type=int, default=6)
    sp.add_argument("--date")
    sp.add_argument("--lane", choices=["theme", "A"], default="theme")
    sp.set_defaults(fn=cmd_purify)
    sp = sub.add_parser("run", help="continuous miner: Lane A first, Lane C (regular) in spare capacity")
    sp.add_argument("--account", required=True)
    sp.add_argument("--hours", type=float, default=8)
    sp.add_argument("--max-sims", type=int, default=300)
    sp.add_argument("--batch", type=int, default=8)
    sp.add_argument("--workers", type=int, default=4)
    sp.add_argument("--c-share", type=float, default=0.25, help="share of each batch for Lane C")
    sp.set_defaults(fn=lambda a: __import__("ppminer.runner", fromlist=["run"]).run(
        DB_PATH, CATALOG_DIR, a.account, a.hours, a.max_sims, a.batch, a.workers, a.c_share))
    sp = sub.add_parser("lanes", help="preview the next runner batch without simulating")
    sp.add_argument("--account", required=True)
    sp.add_argument("-n", type=int, default=12)
    sp.set_defaults(fn=cmd_lanes)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
