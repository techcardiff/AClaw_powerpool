import json, sqlite3

from ppminer import autosubmit, lanes, regular


def chk(prod):
    return json.dumps([{"name": "PROD_CORRELATION", "result": "PASS", "value": prod}])


class FakeBrain:
    def __init__(self):
        self.submitted = []

    def submit(self, aid):
        self.submitted.append(aid)
        return {"submitted": True, "tags": [], "dateSubmitted": "2026-11-20T10:00:00-05:00"}

    def _poll(self, url, max_wait=0):
        return {"max": 0.55}


def _setup(monkeypatch, done_today=0):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    lanes.migrate(conn)
    conn.executescript(autosubmit.SCHEMA)
    regular.migrate(conn)
    conn.execute("create table picks (alpha_id, status, decided_at, note)")
    for i in range(done_today):
        conn.execute("insert into autosubmit_log(account, ny_date, alpha_id, action, detail) values ('A', ?, ?, 'submitted', '')",
                     (str(autosubmit.ny_now().date()), f"d{i}"))
    b = FakeBrain()
    monkeypatch.setattr(autosubmit.Brain, "from_cookie", lambda c: b)
    monkeypatch.setattr(autosubmit.ck, "load_cookie", lambda a: "")

    def pp(conn, catalog, account, dry_run, quality=None, scopes=None, collect=None, collect_max=5):
        collect += [{"lane": "pp", "alpha_id": "P1", "candidate_id": 1, "checks": chk(0.62), "sharpe": 2, "turnover": .1},
                    {"lane": "pp", "alpha_id": "P2", "candidate_id": 2, "checks": "[]", "sharpe": 2, "turnover": .1}]
        return {"status": "done"}

    def rg(conn, account, dry_run, scopes=None, collect=None, log=None, **k):
        collect.append({"lane": "regular", "alpha_id": "R1", "checks": chk(0.41), "sharpe": 1.8, "turnover": .1})
        return {"status": "done"}

    monkeypatch.setattr(autosubmit, "run_account", pp)
    monkeypatch.setattr(autosubmit.regular, "run_account", rg)
    return conn, b


ACFG = {"daily_total": 1, "regular": {"enabled": True}}


def test_submits_only_the_least_correlated(monkeypatch):
    conn, b = _setup(monkeypatch)
    out = autosubmit.daily_one(conn, None, "A", ACFG, {}, dry_run=False)
    assert b.submitted == ["R1"] and out["submitted"] == 1 and out["candidates"] == 3
    # P2 had no PROD_CORRELATION in its check: the correlation endpoint (0.55) filled it in
    detail = conn.execute("select detail from autosubmit_log where action='daily_candidates'").fetchone()[0]
    assert detail.startswith("R1(regular) 0.41, P2(pp) 0.55, P1(pp) 0.62")
    # the next run the same day stops at the daily total
    out2 = autosubmit.daily_one(conn, None, "A", ACFG, {}, dry_run=False)
    assert out2["status"] == "daily_total_reached" and b.submitted == ["R1"]


def test_daily_total_already_used(monkeypatch):
    conn, b = _setup(monkeypatch, done_today=1)
    out = autosubmit.daily_one(conn, None, "A", ACFG, {}, dry_run=False)
    assert out["status"] == "daily_total_reached" and b.submitted == []


def test_dry_run_names_it_without_submitting(monkeypatch):
    conn, b = _setup(monkeypatch)
    out = autosubmit.daily_one(conn, None, "A", ACFG, {}, dry_run=True)
    assert b.submitted == [] and out["least_correlated"] == "R1"


def test_unanswered_prod_correlation_is_retried_then_taken_from_earlier(monkeypatch):
    conn, b = _setup(monkeypatch)
    waits = []

    def slow(url, max_wait=0):
        waits.append((url.split("/")[-3], max_wait))
        raise TimeoutError(url)

    b._poll = slow
    monkeypatch.setattr(autosubmit.regular, "run_account", lambda *a, **k: {"status": "done"})
    # P1's check carries 0.62; P2's endpoint never answers and nothing was seen before: P1 goes in
    out = autosubmit.daily_one(conn, None, "A", ACFG, {}, dry_run=False)
    assert b.submitted == ["P1"] and waits == [("P2", autosubmit.PROD_FIRST_WAIT), ("P2", autosubmit.PROD_SECOND_WAIT)]
    # next day: a value BRAIN gave for P2 earlier counts when the endpoint is silent again
    conn2, b2 = _setup(monkeypatch)
    b2._poll = slow
    monkeypatch.setattr(autosubmit.regular, "run_account", lambda *a, **k: {"status": "done"})
    conn2.executescript(autosubmit.SCHEMA)
    conn2.execute("insert into prod_corr_seen(alpha_id, value) values ('P2', 0.3)")
    autosubmit.daily_one(conn2, None, "A", ACFG, {}, dry_run=False)
    assert b2.submitted == ["P2"]
    assert "[seen " in conn2.execute("select detail from autosubmit_log where action='daily_candidates'").fetchone()[0]
