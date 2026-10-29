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
