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


def test_target_scopes_first_and_already_submitted_skipped(monkeypatch):
    from ppminer import quota
    monkeypatch.setattr(regular, "ny_now", lambda: quota.dt.datetime(2026, 11, 9, 12, tzinfo=quota.NY))
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    c.execute("create table account_submitted (account, alpha_id, expr, canon, stems, tags, sharpe, date_submitted)")
    regular.add(c, [{"account": "A", "alpha_id": a, "fitness": f, "settings": {"region": r, "delay": d}}
                    for a, f, r, d in (("usa1", 3.0, "USA", 1), ("eur", 1.2, "EUR", 1), ("taken", 2.5, "USA", 0),
                                       ("usa0", 1.1, "USA", 0))])

    class B(FakeBrain):
        def alpha(self, aid):
            return {**super().alpha(aid), "status": "ACTIVE" if aid == "taken" else "UNSUBMITTED"}

    b = B({a: chk() for a in ("usa1", "eur", "taken", "usa0")})
    regular.run_account(c, "A", dry_run=False, per_day=4, brain=b, scopes=["EUR/1", "USA/0"])
    assert b.submitted == ["eur", "usa0", "usa1"]          # target scopes first, then the rest; 4 - 0 = room 4
    st = dict(c.execute("select alpha_id, status from regular_queue").fetchall())
    assert st["taken"] == "gone"


def test_resim_old_replaces_strong_and_drops_weak():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    regular.add(c, [{"account": "A", "alpha_id": a, "expr": "rank(close)", "settings": {"region": "USA", "delay": 1,
                     "grade": "x"}, "fitness": 1.5} for a in ("old1", "old2", "fine")])
    c.execute("update regular_queue set detail='fails OLD_SIMULATION=None' where alpha_id in ('old1','old2')")

    class B:
        def __init__(self):
            self.n, self.settings = 0, []

        def simulate(self, expr, st):
            self.n += 1
            self.settings.append(st)
            sh = 2.0 if self.n == 1 else 0.9
            return {"id": f"new{self.n}", "settings": st,
                    "is": {"sharpe": sh, "fitness": 1.3 if sh > 1 else 0.4, "turnover": 0.1, "checks": []}}

    b = B()
    out = regular.resim_old(c, b, "A", workers=1, log=lambda m: None)
    assert out == {"old": 2, "replaced": 1, "dropped": 1, "errors": 0}
    assert "grade" not in b.settings[0]                       # only keys a simulation accepts
    st = dict(c.execute("select alpha_id, status from regular_queue").fetchall())
    assert st == {"old1": "replaced", "old2": "dropped", "fine": "queued", "new1": "queued"}


def test_slow_check_is_retried_after_the_rest_of_the_queue():
    from ppminer import runner
    c = _conn([])

    class Slow(FakeBrain):
        calls = []

        def check(self, aid, max_wait=0):
            self.calls.append((aid, max_wait))
            if aid == "good" and max_wait == runner.CHECK_FIRST_WAIT:
                raise TimeoutError(aid)            # BRAIN still busy on the first try
            return super().check(aid, max_wait)

    b = Slow({"good": chk(), "corr": chk(SELF_CORRELATION="FAIL"), "next": chk()})
    cands = []
    regular.run_account(c, "A", dry_run=False, brain=b, collect=cands)
    assert [x[0] for x in b.calls] == ["good", "corr", "next", "good"]
    assert b.calls[-1][1] == runner.CHECK_SECOND_WAIT
    assert [x["alpha_id"] for x in cands] == ["next", "good"]
