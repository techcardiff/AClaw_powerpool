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
