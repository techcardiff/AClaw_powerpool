import json
import sqlite3

from ppminer import runner
from ppminer.select import shortlist


class SlowBrain:
    def check(self, alpha_id, max_wait=900):
        assert max_wait == runner.CHECK_WAIT_SECONDS
        raise TimeoutError(alpha_id)


def test_check_past_cap_keeps_result_without_checks():
    s = runner.checked(SlowBrain(), {"id": "abc", "is": {"sharpe": 1.7, "fitness": 1.2}})
    assert s["alpha_id"] == "abc" and s["sharpe"] == 1.7 and "checks" not in s


def test_untested_ideas_newest_first():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""create table candidates (id integer primary key, expr text, region text, delay int, fields text,
                 datasets text, categories text, single_dataset int, sharpe real, family_rank int, status text, skeleton text)""")
    for i, (ds, single) in enumerate([("d1", 1), ("d2", 1), ("d3", 0)]):     # the newest one uses a partner dataset
        c.execute("insert into candidates values (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (i + 1, f"rank(f{i})", "USA", 1, json.dumps([f"f{i}"]), json.dumps([ds]), "[]", single, None, None,
                   "explore", f"rank(f{i})"))
    assert [r["id"] for r in shortlist(c, None, n=3, statuses=("explore",))] == [3, 2, 1]


def _jobs_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""create table resims (id integer primary key, candidate_id int, account text, expr text, status text,
                 error text, lane text)""")
    from ppminer import lanes
    c.execute(lanes.MIGRATIONS[-1])
    return c


def test_brain_side_failures_retry_up_to_limit():
    c = _jobs_db()
    msg = "simulation failed: ERROR There was an error while running the simulation. Please try again or contact BRAIN"
    for attempt in range(runner.MAX_RETRIES):
        c.execute("insert into resims(id, candidate_id, account, expr, status, lane) values (1, 7, 'A1', 'rank(x)', 'running', 'A')")
        assert runner.requeue_failed(c, c.execute("select * from resims where id=1").fetchone(), msg)
        assert c.execute("select count(*) from resims").fetchone()[0] == 0        # picked again next batch
    c.execute("insert into resims(id, candidate_id, account, expr, status, lane) values (1, 7, 'A1', 'rank(x)', 'running', 'A')")
    assert not runner.requeue_failed(c, c.execute("select * from resims where id=1").fetchone(), msg)


def test_alpha_errors_and_manual_jobs():
    c = _jobs_db()
    c.execute("insert into resims values (1, 7, 'A1', 'rank(x)', 'running', null, 'A')")
    c.execute("insert into resims values (2, 8, 'A1', 'rank(y)', 'running', null, 'M')")
    assert not runner.requeue_failed(c, c.execute("select * from resims where id=1").fetchone(),
                                     "simulation failed: ERROR Invalid number of inputs : 1, should be exactly 2")
    assert runner.requeue_failed(c, c.execute("select * from resims where id=2").fetchone(), "simulation failed: CANCELLED ")
    assert c.execute("select status from resims where id=2").fetchone()[0] == "queued"


def test_lane_a_caps_template_alphas(monkeypatch):
    from ppminer import lanes
    rows = [{"id": i, "status": "template", "datasets": f'["d{i}"]', "fields": '["t"]'} for i in range(10)] + \
           [{"id": 100 + i, "status": "pp_multi", "datasets": f'["m{i}"]', "fields": '["m"]'} for i in range(10)]
    monkeypatch.setattr(lanes, "collide_stems", lambda c, a: (set(), set()))
    monkeypatch.setattr(lanes, "other_accounts", lambda c, a, o: (set(), set(), set()))
    monkeypatch.setattr(lanes, "submitted_exprs", lambda c, a: set())
    monkeypatch.setattr(lanes, "saturated_datasets", lambda c, a: set())
    monkeypatch.setattr(lanes, "theme_datasets", lambda c, t, a: set())
    monkeypatch.setattr(lanes, "shortlist", lambda c, t, n, statuses, **kw: [r for r in rows if r["status"] in statuses
                                                                            or t is not None])
    picked = lanes.lane_a(None, "A1", 10, theme=object())
    assert sum(r["status"] == "template" for r in picked) == lanes.MAX_TEMPLATES and len(picked) == 10


def test_budget_caps_used_up_fields_across_lanes():
    from ppminer import lanes
    b = lanes.Budget(None)
    b.hidden = [("vwap", 30, 12, 1), ("anl4_adjusted_netincome*", 25, 15, 2)]
    used = {"status": "pp_multi", "fields": '["anl4_adjusted_netincome_ft", "cap"]'}
    fresh = {"status": "pp_multi", "fields": '["fnd6_newa1v1300_ceq"]'}
    assert b.room(fresh) > 100 and b.room(used) == lanes.MAX_USED_UP
    b.take(used, 2)                          # e.g. two funnel fixes of one parent
    b.take(used)
    assert b.room(used) == 0 and b.room(fresh) > 100
    tpl = {"status": "template", "fields": '["vwap"]'}
    assert b.kinds(tpl) == ["template", "used_up"] and b.room(tpl) == 0


def test_top1000_bar_is_lower():
    from ppminer.verdict import quality_gate
    assert quality_gate(1.25, 1.03, 0.08, "[]") == ["sharpe 1.25 < 1.5"]
    assert quality_gate(1.25, 1.03, 0.08, "[]", universe="TOP1000") == []
    assert quality_gate(1.2, 0.85, 0.08, "[]", universe="TOP1000") == ["fitness 0.85 < 0.9"]
    assert quality_gate(1.25, 1.03, 0.08, "[]", {"min_sharpe": 1.6}, universe="TOP1000") == []


def test_transfer_copies_strong_usa_result_where_fields_exist():
    from ppminer import db, transfer
    from ppminer.catalog import Catalog
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA)
    from ppminer import lanes
    lanes.migrate(c)
    c.execute("create table if not exists account_fields (account text, region text, field text, dataset text, category text, type text, description text, alpha_count int, delay int)")
    for region, f in (("USA", "x_a"), ("GLB", "x_a"), ("EUR", "y_b")):
        c.execute("insert into account_fields(account, region, field, dataset, category, type) values ('A1',?,?,'d1','model','MATRIX')", (region, f))
    c.execute("""insert into candidates(id, key, expr, region, delay, fields, status) values (1,'USA|1|rank(x_a)','rank(x_a)','USA',1,'["x_a"]','pp_multi')""")
    good = json.dumps([{"name": "POWER_POOL_CORRELATION", "result": "PASS"}])
    c.execute("""insert into resims(candidate_id, account, expr, settings, alpha_id, sharpe, fitness, turnover, returns, checks, status)
                 values (1,'A1','rank(x_a)',?, 'AL1', 1.9, 1.3, 0.1, 0.1, ?, 'done')""",
              (json.dumps({"region": "USA", "universe": "TOP3000", "delay": 1, "decay": 4, "truncation": 0.08}), good))
    cat = Catalog("/nonexistent")
    cat.add_account_fields(c, "A1")
    assert transfer.ideas(c, cat, "A1") == 1                       # GLB only: EUR lacks x_a, ASI has nothing
    row = c.execute("select * from candidates where status='transfer'").fetchone()
    assert row["region"] == "GLB" and row["decay"] == 4 and "transfer:AL1@A1" in row["sources"]
    assert transfer.ideas(c, cat, "A1") == 0                       # already copied


def test_lane_a_runs_market_copies_past_the_collision_filter(monkeypatch):
    from ppminer import lanes
    rows = [{"id": 1, "status": "transfer", "datasets": '["d"]', "fields": '["x"]'},
            {"id": 2, "status": "transfer", "datasets": '["e"]', "fields": '["y"]'},
            {"id": 3, "status": "transfer", "datasets": '["f"]', "fields": '["z"]'},
            {"id": 10, "status": "pp_multi", "datasets": '["m"]', "fields": '["m"]'}]
    monkeypatch.setattr(lanes, "collide_stems", lambda c, a: ({"x"}, set()))     # the copy's USA original is submitted
    monkeypatch.setattr(lanes, "other_accounts", lambda c, a, o: (set(), {"z"}, set()))
    monkeypatch.setattr(lanes, "submitted_exprs", lambda c, a: set())
    monkeypatch.setattr(lanes, "saturated_datasets", lambda c, a: set())

    def fake_shortlist(c, t, n, statuses, exclude_stems=frozenset(), **kw):
        return [r for r in rows if r["status"] in statuses and r["fields"][2] not in exclude_stems]
    monkeypatch.setattr(lanes, "shortlist", fake_shortlist)
    ids = [r["id"] for r in lanes.lane_a(None, "A1", 5)]
    assert 1 in ids and 2 in ids and 3 not in ids            # other account's family stays out


def test_explore_tops_up_when_queued_ideas_are_unusable(monkeypatch):
    """31 Oct - 2 Nov: hundreds of untested explore rows per region (other account's
    families, saturated datasets) kept Qwen explore off while batches shrank to 3-7."""
    from ppminer import explore, lanes, runner
    calls = []
    monkeypatch.setattr(lanes, "usable_ideas", lambda c, a, o=(), statuses=("explore",): {"GLB": 2, "USA": 40})
    monkeypatch.setattr(explore, "fresh_datasets", lambda c, a, r: [{"dataset": f"{r}_ds"}])
    monkeypatch.setattr(explore, "explore", lambda c, cat, a, ds, r, theme=None: calls.append(r) or [{}])
    monkeypatch.setattr(explore, "save", lambda c, ds, rows, r: len(rows))
    monkeypatch.setattr(runner, "_ro", lambda c: c)
    assert runner.explore_regions(None, None, "A1", ["USA", "GLB"], others=["A1", "B2"]) == 1
    assert calls == ["GLB"]                                  # USA has 40 usable: no top-up


def test_plain_numbers_rewrites_scientific_notation():
    from ppminer.brain import plain_numbers
    assert plain_numbers("x / (ts_std_dev(y, 30) + 1e-8)") == "x / (ts_std_dev(y, 30) + 0.00000001)"
    assert plain_numbers("rank(x) * 2.5E3 - field_1e5") == "rank(x) * 2500 - field_1e5"   # field names untouched


def test_run_jobs_checks_a_chunk_in_parallel_and_keeps_order(monkeypatch):
    import threading, time as _t
    from ppminer import db, runner
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA)
    from ppminer import lanes
    lanes.migrate(c)
    st = json.dumps({"region": "USA", "universe": "TOP3000", "delay": 1})
    ids = [c.execute("insert into resims(candidate_id, account, expr, settings, status) values (?,?,?,?,'queued')",
                     (k, "A1", f"rank(x{k})", st)).lastrowid for k in range(4)]
    live, peak = [0], [0]
    lock = threading.Lock()

    class FakeBrain:
        def multi_simulate(self, items):
            return [{"id": f"AL{k}"} for k in range(len(items))]

    def fake_checked(brain, a):
        with lock:
            live[0] += 1; peak[0] = max(peak[0], live[0])
        _t.sleep(0.05)
        with lock:
            live[0] -= 1
        return {"alpha_id": a["id"], "sharpe": 0.1, "fitness": 0.1, "turnover": 0.1, "returns": 0.01, "drawdown": 0.1,
                "checks": []}
    monkeypatch.setattr(runner, "checked", fake_checked)
    runner.run_jobs(c, FakeBrain(), "A1", ids, workers=4, slots=1)
    assert peak[0] > 1                                         # checks overlapped
    got = {r["id"]: r["alpha_id"] for r in c.execute("select id, alpha_id from resims")}
    assert [got[i] for i in ids] == ["AL0", "AL1", "AL2", "AL3"]   # each result lands on its own job


def test_sims_today_counts_since_midnight_new_york(monkeypatch):
    import datetime as _dt
    from ppminer import scheduler
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(scheduler, "ny_now", lambda: _dt.datetime(2026, 11, 2, 9, 0, tzinfo=ZoneInfo("America/New_York")))
    c = sqlite3.connect(":memory:")
    c.execute("create table resims (account text, created_at text)")
    for ts in ("2026-11-02 04:59:00", "2026-11-02 05:01:00", "2026-11-02 13:00:00"):   # NY midnight = 05:00 UTC
        c.execute("insert into resims values ('A1', ?)", (ts,))
    mx = sqlite3.connect(":memory:")
    mx.execute("create table mx_jobs (account text, created_at text)")
    mx.execute("insert into mx_jobs values ('A1', '2026-11-02 06:00:00')")
    assert scheduler.sims_today(c, "A1") == 2 and scheduler.sims_today(c, "A1", mx) == 3


def test_whole_chunk_bare_fail_reruns_alphas_one_at_a_time(monkeypatch):
    from ppminer import db, lanes, runner
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA); lanes.migrate(c)
    st = json.dumps({"region": "EUR", "universe": "TOP2500", "delay": 1})
    ids = [c.execute("insert into resims(candidate_id, account, expr, settings, status) values (?,?,?,?,'queued')",
                     (k, "A1", f"rank(x{k})", st)).lastrowid for k in range(3)]

    class FakeBrain:
        def multi_simulate(self, items):
            if len(items) > 1:
                return [RuntimeError("simulation failed: FAIL ") for _ in items]
            e = items[0][0]
            return [RuntimeError("simulation failed: ERROR bad field") if e == "rank(x1)" else {"id": "AL" + e[-2]}]
    monkeypatch.setattr(runner, "checked", lambda b, a: {"alpha_id": a["id"], "sharpe": 0.1, "fitness": 0.1, "turnover": 0.1,
                                                          "returns": 0.01, "drawdown": 0.1, "checks": []})
    runner.run_jobs(c, FakeBrain(), "A1", ids, workers=2, slots=1)
    got = {r["id"]: (r["status"], r["alpha_id"], r["error"]) for r in c.execute("select id, status, alpha_id, error from resims")}
    assert got[ids[0]][:2] == ("done", "AL0") and got[ids[2]][:2] == ("done", "AL2")
    assert got[ids[1]][0] == "error" and "bad field" in got[ids[1]][2]


def test_theme_share_skips_templates(monkeypatch):
    from ppminer import lanes
    from ppminer.theme import Theme
    import datetime as _dt
    seen = []

    def fake_shortlist(c, t, n, statuses, **kw):
        if t is not None:
            seen.append(statuses)
        return []
    monkeypatch.setattr(lanes, "shortlist", fake_shortlist)
    monkeypatch.setattr(lanes, "collide_stems", lambda c, a: (set(), set()))
    monkeypatch.setattr(lanes, "other_accounts", lambda c, a, o: (set(), set(), set()))
    monkeypatch.setattr(lanes, "submitted_exprs", lambda c, a: set())
    monkeypatch.setattr(lanes, "saturated_datasets", lambda c, a: set())
    monkeypatch.setattr(lanes, "theme_datasets", lambda c, t, a: set())
    th = Theme(name="GLB/D1 Power Pool Nov`26", start=_dt.date(2026, 10, 26), end=_dt.date(2026, 11, 22), regions={"GLB"})
    lanes.lane_a(None, "A1", 10, theme=th)
    assert seen and all("template" not in st and "explore" in st for st in seen)


def test_brain_pp_check_beats_grouping_under_a_submitted_alpha(monkeypatch):
    from ppminer import picks
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    picks.ensure(c)
    ok = json.dumps([{"name": "POWER_POOL_CORRELATION", "result": "PASS"}])
    row = lambda aid, sh, fi, chk, rid: {"alpha_id": aid, "sharpe": sh, "fitness": fi, "turnover": 0.05, "single_dataset": 0,
                                        "checks": chk, "id": rid, "candidate_id": rid}
    sub = row("SUB", 1.37, 0.79, ok, 1)
    glb = row("GLB", 1.2, 0.81, ok, 2)                      # BRAIN: Power Pool correlation PASS
    pend = row("PEND", 1.19, 0.8, "[]", 3)                  # not checked yet
    monkeypatch.setattr(picks, "passing", lambda conn, a: [sub, glb, pend])
    monkeypatch.setattr(picks, "daily", lambda conn, aid: aid)
    monkeypatch.setattr(picks, "corr", lambda a, b: 0.51)
    c.execute("""insert into picks(alpha_id, resim_id, candidate_id, account, cluster, score, status)
                 values ('SUB', 1, 1, 'A1', 1, 1.37, 'submitted')""")
    out = {p["alpha_id"]: p for p in picks.build(c, "A1")}
    assert "GLB" in out                                      # no longer folded under the submitted alpha
    assert "PEND" not in out and ["PEND", 0.51] in out["GLB"]["members"] + out["SUB"]["members"]


def test_submit_refusal_names_the_checks_instead_of_raising(monkeypatch):
    from ppminer import brain as br
    monkeypatch.setattr(br.sandbox, "ON", False)

    class R:
        status_code = 403
        text = '{"is": {"checks": [...]}}'
        def json(self):
            return {"is": {"checks": [{"name": "LOW_SHARPE", "result": "FAIL"}, {"name": "SELF_CORRELATION", "result": "PENDING"},
                                      {"name": "LOW_FITNESS", "result": "PASS"}]}}
    b = br.Brain()
    monkeypatch.setattr(b.s, "post", lambda *a, **k: R())
    res = b.submit("1YNk2wwX")
    assert res["submitted"] is False and res["refused"] == ["LOW_SHARPE=FAIL", "SELF_CORRELATION=PENDING"]


def _jobs(c, exprs, region="GLB"):
    from ppminer import db, lanes
    c.executescript(db.SCHEMA); lanes.migrate(c)
    st = json.dumps({"region": region, "universe": "TOP3000", "delay": 1})
    return [c.execute("insert into resims(candidate_id, account, expr, settings, status) values (?,?,?,?,'queued')",
                      (k, "A1", e, st)).lastrowid for k, e in enumerate(exprs)]


def test_heavy_multisim_is_split_until_the_heavy_alpha_is_alone(monkeypatch):
    """Ported from Song's songjianalpha (814bf67)."""
    from ppminer import runner
    heavy = RuntimeError("simulation failed: ERROR Your simulation probably took too much resource.")
    calls = []

    class FakeBrain:
        def multi_simulate(self, items):
            calls.append(len(items))
            if len(items) > 1 and any(e == "heavy(x)" for e, _ in items):
                return [heavy] * len(items)
            return [heavy if e == "heavy(x)" else {"id": e} for e, _ in items]
    monkeypatch.setattr(runner, "checked", lambda b, a: {"alpha_id": a["id"], "sharpe": 1.2, "fitness": 1.0, "turnover": 0.1,
                                                          "returns": 0.1, "drawdown": 0.05, "checks": []})
    monkeypatch.setattr(runner, "log", lambda *a: None)
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    ids = _jobs(c, [f"rank(f{i})" for i in range(9)] + ["heavy(x)"])
    st = runner.run_jobs(c, FakeBrain(), "A1", ids, workers=2, slots=1)
    assert (st["done"], st["error"]) == (9, 1)
    assert calls[0] == 10 and len(calls) <= 1 + 2 * 4                # halving: at most ~2 calls per level
    assert [tuple(r) for r in c.execute("select expr from resims where status='error'")] == [("heavy(x)",)]


def test_other_multisim_failures_are_not_split(monkeypatch):
    from ppminer import runner
    calls = []

    class FakeBrain:
        def multi_simulate(self, items):
            calls.append(len(items))
            return [RuntimeError("simulation failed: ERROR Invalid data field foo")] * len(items)
    monkeypatch.setattr(runner, "log", lambda *a: None)
    monkeypatch.setattr(runner, "block", lambda *a: None)
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    ids = _jobs(c, [f"rank(g{k})" for k in range(3)])
    assert runner.run_jobs(c, FakeBrain(), "A1", ids, workers=1, slots=1)["error"] == 3 and calls == [3]


def test_cookie_sync_retries_a_closed_connection(monkeypatch, tmp_path):
    from ppminer import cookies as ck
    tries = []

    class P:
        def __init__(self, rc): self.returncode, self.stderr = rc, "scp: Connection closed"
    monkeypatch.setenv("PP_COOKIE_SYNC", "host:/dir/")
    monkeypatch.setattr(ck.time, "sleep", lambda s: None)
    monkeypatch.setattr(ck.subprocess, "run", lambda *a, **k: tries.append(1) or P(0 if len(tries) == 3 else 1))
    assert ck.sync_cookie(str(tmp_path / "x.cookie")) is True and len(tries) == 3
    tries.clear()
    monkeypatch.setattr(ck.subprocess, "run", lambda *a, **k: tries.append(1) or P(1))
    assert ck.sync_cookie(str(tmp_path / "x.cookie")) is False and len(tries) == 1 + len(ck.SYNC_DELAYS)


def test_cancelled_neighbours_are_resent_in_the_same_batch(monkeypatch):
    from ppminer import runner
    bad = RuntimeError('simulation failed: ERROR Incompatible unit for input of "group_neutralize" at index 1')
    calls = []

    class FakeBrain:
        def multi_simulate(self, items):
            calls.append(len(items))
            if any(e == "bad(x)" for e, _ in items):
                return [bad if e == "bad(x)" else RuntimeError("simulation failed: CANCELLED ") for e, _ in items]
            return [{"id": e} for e, _ in items]
    monkeypatch.setattr(runner, "checked", lambda b, a: {"alpha_id": a["id"], "sharpe": 1.0, "fitness": 1.0, "turnover": 0.1,
                                                          "returns": 0.1, "drawdown": 0.05, "checks": []})
    monkeypatch.setattr(runner, "log", lambda *a: None)
    monkeypatch.setattr(runner, "block", lambda *a: None)
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    ids = _jobs(c, ["rank(a)", "bad(x)", "rank(b)", "rank(c)"])
    st = runner.run_jobs(c, FakeBrain(), "A1", ids, workers=2, slots=1)
    assert (st["done"], st["error"], st["retry"]) == (3, 1, 0) and calls == [4, 3]


def test_scheduler_accounts_do_not_wait_for_each_other(monkeypatch, tmp_path):
    """3 Nov: a pass waited for both accounts, so HT52122 idled 43% of the time."""
    import threading, time as _t
    from ppminer import scheduler as sch
    passes = {"FAST": 0, "SLOW": 0}
    lock = threading.Lock()

    def fake_cycle(conn, catalog, account, **kw):
        _t.sleep(0.02 if account == "FAST" else 0.6)
        with lock:
            passes[account] += 1
        return {"status": "ok", "sims": 1}
    monkeypatch.setattr(sch, "cycle", fake_cycle)
    monkeypatch.setattr(sch.autosubmit, "load_config", lambda: {"accounts": {"FAST": {"enabled": True}, "SLOW": {"enabled": True}}})
    monkeypatch.setattr(sch.th, "current", lambda conn: None)
    monkeypatch.setattr(sch, "_due", lambda conn, a, cfg: False)
    monkeypatch.setattr(sch, "_brain", lambda a: None)
    monkeypatch.setattr(sch, "sims_today", lambda c, a, mx=None: 0)
    monkeypatch.setattr(sch.marketx, "clear_unfinished", lambda mx: 0)
    monkeypatch.setattr(sch.marketx, "connect", lambda: sqlite3.connect(":memory:"))
    monkeypatch.setattr(sch, "Catalog", lambda d: type("C", (), {"add_account_fields": lambda self, c, a: None})())
    monkeypatch.setattr(sch, "PASS_POLL_SECONDS", 0.01)
    monkeypatch.setattr(sch, "log", lambda *a: None)
    sch.run(str(tmp_path / "pp.db"), str(tmp_path), hours=0.7 / 3600)
    assert passes["SLOW"] >= 1 and passes["FAST"] >= 5 * passes["SLOW"]


def test_brain_retries_server_errors_then_gives_up(monkeypatch):
    import requests
    from ppminer import brain as br
    monkeypatch.setattr(br.time, "sleep", lambda s: None)
    codes = [500, 502, 200]

    class R:
        def __init__(self, code): self.status_code, self.headers = code, {}
        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(f"{self.status_code} Server Error")
    b = br.Brain()
    monkeypatch.setattr(b.s, "request", lambda *a, **k: R(codes.pop(0)))
    assert b._req("GET", "https://x/alphas/A1").status_code == 200
    monkeypatch.setattr(b.s, "request", lambda *a, **k: R(500))
    try:
        b._req("GET", "https://x/alphas/A1")
        raise AssertionError("should give up")
    except requests.HTTPError:
        pass
