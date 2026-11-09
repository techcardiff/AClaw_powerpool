import json, sqlite3

from ppminer import regular


def chk(**res):
    base = {"LOW_SHARPE": "PASS", "SELF_CORRELATION": "PASS", "PROD_CORRELATION": "PASS",
            "POWER_POOL_CORRELATION": "FAIL", "MATCHES_THEMES": "FAIL"}
    base.update(res)
    return [{"name": n, "result": r, "value": 0.5} for n, r in base.items()]


def test_judge_ignores_power_pool_and_theme_but_needs_correlation():
    assert regular.judge(chk()) == (True, "")
    assert not regular.judge(chk(SELF_CORRELATION="PENDING"))[0]
    assert not regular.judge(chk(PROD_CORRELATION="FAIL"))[0]
    assert not regular.judge(chk(LOW_SHARPE="FAIL"))[0]
    assert not regular.judge([c for c in chk() if c["name"] != "PROD_CORRELATION"])[0]


class FakeBrain:
    def __init__(self, checks):
        self.checks, self.submitted = checks, []

    def is_authenticated(self):
        return True

    def alpha(self, aid):
        return {"id": aid, "is": {"sharpe": 2.0, "fitness": 1.5, "turnover": 0.1}}

    def check(self, aid, max_wait=0):
        return {"is": {"checks": self.checks[aid]}}

    def submit(self, aid):
        self.submitted.append(aid)
        return {"submitted": True, "tags": [], "dateSubmitted": "2026-11-09T10:00:00-05:00"}


def _conn(today_dates):
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    c.execute("create table account_submitted (account, alpha_id, expr, canon, stems, tags, sharpe, date_submitted)")
    for i, (tags, d) in enumerate(today_dates):
        c.execute("insert into account_submitted values ('A', ?, '', '', '', ?, 1, ?)", (f"s{i}", json.dumps(tags), d))
    regular.add(c, [{"account": "A", "alpha_id": a, "fitness": f} for a, f in (("good", 2.0), ("corr", 1.9), ("next", 1.5))])
    return c


def test_submits_best_passing_alphas_within_the_daily_limit(monkeypatch):
    from ppminer import quota
    monkeypatch.setattr(regular, "ny_now", lambda: quota.dt.datetime(2026, 11, 9, 12, tzinfo=quota.NY))
    # two Power Pool submissions today already: 4-per-day limit leaves room for 2
    c = _conn([(["PowerPoolSelected"], "2026-11-09T01:00:00-05:00")] * 2 + [([], "2026-11-08T09:00:00-05:00")])
    b = FakeBrain({"good": chk(), "corr": chk(SELF_CORRELATION="FAIL"), "next": chk()})
    r = regular.run_account(c, "A", dry_run=False, per_day=3, brain=b)
    assert b.submitted == ["good", "next"] and r["room"] == 2
    st = dict(c.execute("select alpha_id, status from regular_queue").fetchall())
    assert st == {"good": "submitted", "corr": "queued", "next": "submitted"}


def test_dry_run_never_submits(monkeypatch):
    c = _conn([])
    b = FakeBrain({"good": chk(), "corr": chk(), "next": chk()})
    r = regular.run_account(c, "A", dry_run=True, per_day=2, brain=b)
    assert b.submitted == [] and r["submitted"] == 2
