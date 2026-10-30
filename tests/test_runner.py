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
