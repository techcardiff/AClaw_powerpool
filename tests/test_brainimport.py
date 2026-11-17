import json, os, tempfile

from ppminer import brainimport, db, lanes
from ppminer.catalog import Catalog, FieldInfo


def alpha(aid, code, sharpe=2.0, fitness=1.3, turnover=0.2, fails=()):
    return {"id": aid, "regular": {"code": code},
            "settings": {"region": "USA", "delay": 1, "universe": "TOP3000", "neutralization": "SUBINDUSTRY",
                         "decay": 4, "truncation": 0.08},
            "is": {"sharpe": sharpe, "fitness": fitness, "turnover": turnover, "returns": 0.1, "drawdown": 0.05,
                   "checks": [{"name": n, "result": "FAIL"} for n in fails]}}


class Resp:
    def __init__(self, j):
        self.j = j

    def json(self):
        return self.j


class FakeBrain:
    def __init__(self, alphas):
        self.alphas, self.calls = alphas, []

    def is_authenticated(self):
        return True

    def _req(self, method, url, params=None, **kw):
        self.calls.append(params)
        off, lim = params["offset"], params["limit"]
        return Resp({"count": len(self.alphas), "results": self.alphas[off:off + lim]})


def _cat():
    cat = Catalog("/nonexistent")
    for f in ("close", "volume", "open", "high"):
        cat.by_region.setdefault("USA", {})[f] = FieldInfo(f, "pv", "pv1", "MATRIX", "USA")
    return cat


def _conn():
    c = db.connect(os.path.join(tempfile.mkdtemp(), "t.db"))
    lanes.migrate(c)
    return c


def test_imports_strong_power_pool_shapes_once():
    good = "rank(ts_mean(close, 20))"
    br = FakeBrain([alpha("A1", good, sharpe=2.5),
                    alpha("A2", "rank(ts_delta(close, 5))", sharpe=2.0, turnover=0.6),          # turnover above the bar
                    alpha("A3", "rank(ts_mean(volume, 10))", sharpe=1.9, fails=["LOW_SUB_UNIVERSE_SHARPE"]),
                    alpha("A4", "rank(ts_mean(open, 10))", sharpe=1.8, fitness=0.7),             # fitness below the bar
                    alpha("A5", "rank(ts_mean(high, 10))", sharpe=1.2)])                          # below min Sharpe: stop
    c, cat = _conn(), _cat()
    out = brainimport.run(c, cat, "HT52122", brain=br)
    assert out == {"status": "done", "seen": 5, "added": 1}
    r = c.execute("select * from resims").fetchone()
    assert (r["alpha_id"], r["status"], r["lane"], r["account"]) == ("A1", "done", "I", "HT52122")
    assert json.loads(r["settings"])["universe"] == "TOP3000"
    assert c.execute("select status from candidates where id=?", (r["candidate_id"],)).fetchone()[0] == "imported"
    assert br.calls[0]["status"] == "UNSUBMITTED" and br.calls[0]["order"] == "-is.sharpe"
    # a second run adds nothing: the alpha is already in
    assert brainimport.run(c, cat, "HT52122", brain=br)["added"] == 0


def test_per_account_cap():
    br = FakeBrain([alpha(f"B{i}", f"rank(ts_mean(close, {10 + i}))") for i in range(5)])
    c = _conn()
    assert brainimport.run(c, _cat(), "YO50689", per_account=2, brain=br)["added"] == 2


def test_fresh_database_has_no_field_table_yet():
    c = _conn()
    assert Catalog("/nonexistent").add_account_fields(c, "HT52122") == 0
