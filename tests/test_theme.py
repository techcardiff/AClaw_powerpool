import json
import sqlite3

from ppminer import theme as th
from ppminer.verdict import verdict

NOV = {"id": "9B3qVP4", "multiplier": 1.0, "name": "GLB/D1 Power Pool Nov`26"}


def _mt(result, themes):
    return [{"name": "MATCHES_THEMES", "result": result, "themes": themes}]


def test_warning_lists_unmatched_themes():
    assert th.matched_pp_themes(json.dumps(_mt("WARNING", [NOV]))) == []
    assert th.matched_pp_themes(_mt("PASS", [NOV, {"name": "USA/D1 TOP1000 Theme"}])) == ["GLB/D1 Power Pool Nov`26"]
    assert th.matched_pp_themes(None) == []


def test_verdict_requires_a_matched_theme():
    checks = [{"name": "POWER_POOL_CORRELATION", "result": "PASS", "value": 0.2}] + _mt("WARNING", [NOV])
    assert "no Power Pool theme" in verdict(1.6, json.dumps(checks), require_theme=True)[1]


def test_rule_from_theme_name():
    t = th.from_name("GLB/D1 Power Pool Nov`26")
    assert t.regions == {"GLB"} and t.delay == 1
    assert t.matches(delay=1, region="GLB", categories={"pv"})
    assert not t.matches(delay=1, region="USA", categories={"pv"})
    assert th.from_name("D1 Power Pool Oct`26 2").regions == set()


def test_detect_reads_newest_check():
    c = sqlite3.connect(":memory:")
    c.execute("create table resims (id integer primary key, status text, checks text)")
    old = {"name": "D1 Power Pool Oct`26 2"}
    c.execute("insert into resims(status, checks) values ('done', ?)", (json.dumps(_mt("WARNING", [old])),))
    c.execute("insert into resims(status, checks) values ('done', ?)", (json.dumps(_mt("WARNING", [NOV])),))
    assert th.detect(c).regions == {"GLB"}
    c.execute("delete from resims where id = 2")
    assert th.detect(c) is None             # name carries no rule and no themes.json entry is active


def test_theme_datasets_of_other_account():
    from ppminer.lanes import theme_datasets
    c = sqlite3.connect(":memory:")
    c.executescript("""create table candidates (id integer primary key, region text, datasets text);
                       create table resims (id integer primary key, candidate_id int, account text);
                       insert into candidates values (1, 'GLB', '["model257"]'), (2, 'USA', '["analyst4"]');
                       insert into resims(candidate_id, account) values (1, 'YO50689'), (2, 'YO50689');""")
    t = th.from_name("GLB/D1 Power Pool Nov`26")
    assert theme_datasets(c, t, ["YO50689"]) == {'["model257"]'}
    assert theme_datasets(c, t, []) == set()
