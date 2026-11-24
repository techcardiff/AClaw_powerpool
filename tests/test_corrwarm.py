import json
import sqlite3

from ppminer import autosubmit, corrwarm, db, regular


def _conn():
    c = sqlite3.connect(":memory:")
    c.executescript(db.SCHEMA)
    c.executescript(autosubmit.SCHEMA)
    regular.migrate(c)
    c.execute("create table if not exists runner_state (key text primary key, value text, updated_at timestamp)")
    c.execute("create table if not exists account_submitted (account, alpha_id, expr, canon, stems, tags, sharpe, date_submitted)")
    rows = [("A1", "skip", "self-correlation still computing at BRAIN; next run", 3.1),
            ("A2", "regular_skip", "SELF_CORRELATION PENDING", 5.3),
            ("A3", "skip", "below quality bar: sharpe 1.2 < 1.5", 1.2),
            ("A4", "skip", "production correlation unknown; next run", 2.0),
            ("A5", "skip", "self-correlation still computing at BRAIN; next run", 2.5)]
    for aid, action, detail, sh in rows:
        c.execute("insert into autosubmit_log(account, ny_date, alpha_id, action, detail) values ('YO', '2026-11-24', ?, ?, ?)",
                  (aid, action, detail))
        c.execute("insert into resims(account, alpha_id, sharpe, status) values ('YO', ?, ?, 'done')", (aid, sh))
    c.execute("insert into account_submitted values ('YO', 'A5', '', '', '', '[]', 2.5, '2026-11-24T01:00:00-05:00')")
    c.execute("insert into runner_state(key, value) values ('autosubmit:YO', '\"x\"')")
    c.commit()
    return c


class B:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def _poll(self, url, max_wait=0):
        aid, kind = url.split("/")[-3], url.split("/")[-1]
        self.calls.append((aid, kind, max_wait))
        v = self.answers.get((aid, kind))
        if v is None:
            raise TimeoutError(url)
        return v


SELF = {"schema": {"properties": [{"name": "id"}, {"name": "correlation"}]}, "records": [["X", 0.41], ["Y", -0.55]]}


def test_targets_are_the_stuck_ones_best_first_not_submitted():
    assert corrwarm.targets(_conn(), "YO") == ["A2", "A1", "A4"]


def test_answers_are_stored_pass_made_due_and_not_asked_again_soon():
    c, logs = _conn(), []
    b = B({("A2", "self"): SELF, ("A2", "prod"): {"max": 0.62}})
    assert corrwarm.round_for(c, b, "YO", logs.append, wait=5) == 1
    assert [x[:2] for x in b.calls] == [("A2", "self"), ("A2", "prod"), ("A1", "self"), ("A1", "prod"),
                                        ("A4", "self"), ("A4", "prod")]
    assert c.execute("select value from corr_warm where alpha_id='A2' and kind='self'").fetchone()[0] == 0.55
    assert c.execute("select value from prod_corr_seen where alpha_id='A2'").fetchone()[0] == 0.62
    assert c.execute("select status from corr_warm where alpha_id='A1' and kind='self'").fetchone()[0] == "timeout"
    assert c.execute("select 1 from runner_state where key='autosubmit:YO'").fetchone() is None
    assert any("submit pass made due" in m for m in logs)
    assert corrwarm.targets(c, "YO") == []          # all tried within RETRY_HOURS


def test_nothing_answered_leaves_the_pass_schedule_alone():
    c = _conn()
    assert corrwarm.round_for(c, B({}), "YO", lambda m: None, wait=5) == 0
    assert c.execute("select 1 from runner_state where key='autosubmit:YO'").fetchone() is not None


def test_daily_pick_uses_the_fetched_prod_value(monkeypatch):
    c = _conn()
    corrwarm.warm(c, B({("A1", "prod"): {"max": 0.33}}), "YO", "A1", wait=5)
    seen = c.execute("select value from prod_corr_seen where alpha_id='A1'").fetchone()[0]
    assert seen == 0.33
