import sqlite3

from ppminer import superalpha


def c(name, result, value=None, limit=None):
    return {"name": name, "result": result, "value": value, "limit": limit}


BASE_OK = [c("LOW_SHARPE", "PASS", 5.8, 1.58), c("SELF_CORRELATION", "PASS", 0.3), c("PROD_CORRELATION", "PASS", 0.4),
           c("SUPER_SUBMISSION", "PASS", 0, 1), c("OSMOSIS_ALLOCATION", "WARNING"), c("MATCHES_COMPETITION", "WARNING")]


def test_describe_is_long_enough_and_follows_the_expressions():
    st = superalpha.settings()
    sel, combo = superalpha.describe("and(self_correlation < 0.4, turnover < 0.3)", 'combo_a(alpha, nlength=500)', st)
    assert len(sel) >= 100 and len(combo) >= 100
    assert "correlation with my other submitted alphas below 0.4" in sel and "daily turnover below 0.3" in sel
    assert "500 trading days" in combo
    _, combo2 = superalpha.describe("1", 'combo_a(alpha, mode="algo2")', st)
    assert "250 trading days" in combo2 and "algo2" in combo2


def test_judge():
    assert superalpha.judge(BASE_OK) == ("ok", "")
    pend = [x if x["name"] != "SELF_CORRELATION" else c("SELF_CORRELATION", "PENDING") for x in BASE_OK]
    assert superalpha.judge(pend)[0] == "wait"
    full = [x if x["name"] != "SUPER_SUBMISSION" else c("SUPER_SUBMISSION", "FAIL", 1, 1) for x in BASE_OK]
    assert superalpha.judge(full)[0] == "done_today"
    assert superalpha.judge(BASE_OK + [c("COMBO_DESCRIPTION_LENGTH", "FAIL", 0, 100)])[0] == "fail"


class FakeBrain:
    def __init__(self):
        self.submitted, self.patched, self.posted = [], [], 0

    def is_authenticated(self):
        return True

    def _req(self, method, url, json=None, **kw):
        if method == "PATCH":
            self.patched.append(json)
        return type("R", (), {"headers": {"Location": "loc"}, "json": lambda s: {}})()

    def _poll(self, url, max_wait=0):
        self.posted += 1
        return {"alpha": f"S{self.posted}"}

    def alpha(self, aid):
        return {"id": aid, "is": {"sharpe": 5.0 + self.posted / 10, "fitness": 6.0 + self.posted / 10,
                                  "turnover": 0.15, "checks": []}}

    def check(self, aid, max_wait=0):
        return {"is": {"checks": BASE_OK + [c("COMBO_DESCRIPTION_LENGTH", "FAIL", 0, 100)]}}

    def submit(self, aid):
        self.submitted.append(aid)
        return {"submitted": True, "dateSubmitted": "2026-11-18T10:00:00-05:00"}


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def test_dry_run_simulates_the_grid_and_names_the_best_without_submitting():
    conn, b = _conn(), FakeBrain()
    out = superalpha.run_account(conn, "HT52122", dry_run=True, brain=b)
    assert out["simulated"] == len(superalpha.SELECTIONS) * len(superalpha.COMBOS) * len(superalpha.SCOPES)
    assert out["would_submit"] and b.submitted == [] and b.patched == []


def test_live_sets_descriptions_and_submits_one():
    conn, b = _conn(), FakeBrain()

    class Live(FakeBrain):
        def check(self, aid, max_wait=0):
            return {"is": {"checks": BASE_OK}}

    b = Live()
    out = superalpha.run_account(conn, "HT52122", dry_run=False, brain=b)
    assert out["submitted"] == 1 and len(b.submitted) == 1
    assert {"selection", "combo"} <= set(b.patched[0])
    assert len(b.patched[0]["selection"]["description"]) >= 100
    st = conn.execute("select status from super_alphas where alpha_id=?", (b.submitted[0],)).fetchone()[0]
    assert st == "submitted"
    # simulated variants are reused the next day; nothing new is simulated
    out2 = superalpha.run_account(conn, "HT52122", dry_run=False, brain=b)
    assert out2["simulated"] == 0


def test_pending_check_tries_when_correlation_endpoints_pass():
    pend = [x if x["name"] not in ("SELF_CORRELATION", "PROD_CORRELATION") else c(x["name"], "PENDING") for x in BASE_OK]

    class B(FakeBrain):
        def check(self, aid, max_wait=0):
            return {"is": {"checks": pend}}

        def _poll(self, url, max_wait=0):
            if "/correlations/" in url:
                return {"max": 0.48 if url.endswith("self") else 0.64}
            return super()._poll(url, max_wait)

    conn, b = _conn(), B()
    out = superalpha.run_account(conn, "HT52122", dry_run=False, brain=b)
    assert out["submitted"] == 1

    class High(B):
        def _poll(self, url, max_wait=0):
            if "/correlations/" in url:
                return {"max": 0.75}
            return FakeBrain._poll(self, url, max_wait)

    conn, b = _conn(), High()
    out = superalpha.run_account(conn, "HT52122", dry_run=False, brain=b)
    assert out["submitted"] == 0 and b.submitted == []
    # twins of an earlier super alpha are dropped, and the same grid isn't simulated again
    assert conn.execute("select count(*) from super_alphas where status='simulated'").fetchone()[0] == 0
    out = superalpha.run_account(conn, "HT52122", dry_run=False, brain=b)
    assert out["simulated"] == 0


def test_unanswered_old_variants_dont_hold_back_the_grid():
    class Slow(FakeBrain):
        def check(self, aid, max_wait=0):
            assert max_wait <= superalpha.CHECK_WAIT
            raise TimeoutError(aid)

        def _poll(self, url, max_wait=0):
            if "/correlations/" in url:
                raise TimeoutError(url)
            return super()._poll(url, max_wait)

    conn, b = _conn(), Slow()
    superalpha.migrate(conn)
    conn.execute("insert into super_alphas(account, alpha_id, selection, combo, settings, fitness, checks) "
                 "values ('HT52122', 'OLD1', '1', 'combo_a(alpha)', ?, 9, '[]')", ('{"region": "USA", "delay": 1}',))
    out = superalpha.run_account(conn, "HT52122", dry_run=False, brain=b)
    n = len(superalpha.SELECTIONS) * len(superalpha.COMBOS) * len(superalpha.SCOPES)
    assert out["simulated"] == n and out["submitted"] == 0
    old = conn.execute("select status, detail, checks from super_alphas where alpha_id='OLD1'").fetchone()
    assert old["status"] == "simulated" and old["checks"] == "[]" and "still running" in old["detail"]
    assert superalpha.run_account(conn, "HT52122", dry_run=False, brain=b)["simulated"] == 0


def test_check_timeout_still_tries_when_correlations_pass():
    class B(FakeBrain):
        def check(self, aid, max_wait=0):
            raise TimeoutError(aid)

        def _poll(self, url, max_wait=0):
            if "/correlations/" in url:
                return {"max": 0.5}
            return super()._poll(url, max_wait)

    conn, b = _conn(), B()
    assert superalpha.run_account(conn, "HT52122", dry_run=False, brain=b)["submitted"] == 1


def test_brain_error_variants_are_not_resent_every_pass(monkeypatch):
    sent = []

    def fail(brain, sel, combo, st, max_wait=0):
        sent.append((sel, combo, st["region"]))
        raise RuntimeError("super simulation failed: ERROR At least 10 component alphas are required for Super Alpha.")

    monkeypatch.setattr(superalpha, "simulate", fail)
    conn, b = _conn(), FakeBrain()
    n = len(superalpha.SELECTIONS) * len(superalpha.COMBOS) * len(superalpha.SCOPES)
    assert superalpha.run_account(conn, "HT52122", dry_run=False, brain=b)["simulated"] == 0 and len(sent) == n
    superalpha.run_account(conn, "HT52122", dry_run=False, brain=b)
    assert len(sent) == n and b.submitted == []


def test_too_few_components_is_retried_after_a_day(monkeypatch):
    sent = []

    def fail(brain, sel, combo, st, max_wait=0):
        sent.append(st["region"])
        raise RuntimeError("super simulation failed: ERROR At least 10 component alphas are required for Super Alpha.")

    monkeypatch.setattr(superalpha, "simulate", fail)
    conn, b = _conn(), FakeBrain()
    n = len(superalpha.SELECTIONS) * len(superalpha.COMBOS) * len(superalpha.SCOPES)
    superalpha.run_account(conn, "HT52122", dry_run=False, brain=b)
    conn.execute("update super_alphas set created_at=datetime('now', '-2 days')")
    superalpha.run_account(conn, "HT52122", dry_run=False, brain=b)
    assert len(sent) == 2 * n


def test_no_records_means_nothing_to_collide_with():
    class B(FakeBrain):
        def _poll(self, url, max_wait=0):
            if url.endswith("/self"):
                return {"max": None, "min": None, "records": []}
            if url.endswith("/prod"):
                return {"max": 0.69, "records": [{}]}
            return super()._poll(url, max_wait)

    assert superalpha.correlations(B(), "X") == {"self": 0.0, "prod": 0.69}
