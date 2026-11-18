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
    assert out["simulated"] == len(superalpha.SELECTIONS) * len(superalpha.COMBOS)
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
