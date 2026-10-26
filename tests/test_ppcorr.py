from ppminer import ppcorr


class FakeBrain:
    def __init__(self, records):
        self.records = records

    def _poll(self, url, max_wait=0):
        props = [{"name": n} for n in ("id", "name", "instrumentType", "region", "universe", "correlation", "sharpe")]
        return {"schema": {"properties": props}, "records": [[a, None, "EQUITY", "USA", "TOP3000", c, 2.0]
                                                             for a, c in self.records]}


def test_power_pool_alpha_in_top5_gives_its_value():
    v, _ = ppcorr.estimate(FakeBrain([("x", 0.79), ("pp1", 0.62), ("y", 0.4)]), "a", {"pp1"})
    assert v == 0.62


def test_no_pp_in_top5_uses_smallest_as_upper_bound():
    v, how = ppcorr.estimate(FakeBrain([("x", 0.79), ("y", 0.45), ("z", 0.31)]), "a", {"pp1"})
    assert v == 0.31 and "upper bound" in how


def test_unknown_when_all_top5_high_and_no_pp():
    v, _ = ppcorr.estimate(FakeBrain([("x", 0.79), ("y", 0.6)]), "a", {"pp1"})
    assert v is None


def test_fill_only_when_missing():
    present = [{"name": "POWER_POOL_CORRELATION", "result": "PASS"}]
    assert ppcorr.fill(None, None, "a", present) is present
