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


def test_amer_rule_and_sub_sharpes():
    t = th.parse_theme("GLB/D1 Power Pool Nov`26", "2026-10-26", "2026-11-29", "region=GLB & delay=1 & AMER Sharpe > 1")
    assert t.regions == {"GLB"} and t.delay == 1 and t.sub_sharpe == {"AMER": 1.0}
    checks = [{"name": "LOW_GLB_AMER_SHARPE", "result": "FAIL", "value": 0.58},
              {"name": "LOW_GLB_APAC_SHARPE", "result": "FAIL", "value": 0.17}]
    assert th.sub_sharpes(json.dumps(checks)) == {"AMER": 0.58, "APAC": 0.17}


def test_revise_ranks_theme_rows_by_amer_sharpe():
    from ppminer import revise
    t = th.parse_theme("GLB/D1 Power Pool Nov`26", "2026-10-26", "2026-11-29", "region=GLB & delay=1 & AMER Sharpe > 1")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""create table candidates (id integer primary key, status text, region text, depth int, parent_id int);
                       create table resims (id integer primary key, candidate_id int, account text, status text,
                                            sharpe real, fitness real, turnover real, checks text);
                       create table llm_jobs (candidate_id int, kind text);""")
    amer = lambda v: json.dumps([{"name": "LOW_GLB_AMER_SHARPE", "result": "FAIL", "value": v}])
    for cid, status, region, sharpe, checks in ((1, "explore", "USA", 0.9, "[]"), (2, "template", "GLB", 0.4, amer(0.6)),
                                                 (3, "template", "GLB", 0.7, amer(0.8)), (4, "template", "GLB", 0.5, amer(0.2)),
                                                 (5, "template", "USA", 0.9, "[]")):
        c.execute("insert into candidates values (?,?,?,0,null)", (cid, status, region))
        c.execute("insert into resims(candidate_id, account, status, sharpe, checks) values (?, 'A', 'done', ?, ?)", (cid, sharpe, checks))
    assert [r["id"] for r in revise.todo(c, "A", t)] == [3, 2, 1]   # AMER 0.2 is too far; USA templates stay out
    assert [r["id"] for r in revise.todo(c, "A")] == [1]


GLB_RULE = "region=GLB & delay=1 & AMER Sharpe > 1"
NOV_NAME = NOV["name"]


def _checks(amer, themes_pass=False):
    out = [{"name": "LOW_GLB_AMER_SHARPE", "result": "PASS" if amer > 1 else "FAIL", "limit": 1, "value": amer}]
    if themes_pass:      # what BRAIN reports once the alpha has a description (3qorW3L6, 28 Oct)
        out += [{"name": "MATCHES_THEMES", "result": "WARNING", "themes": [{"name": "DEU/D1 GBR/D1 Theme"}]},
                {"name": "MATCHES_THEMES", "result": "PASS", "themes": [{"name": NOV_NAME}, {"name": "GLB/D1/MODEL Pyramid Theme"}]}]
    else:
        out += [{"name": "MATCHES_THEMES", "result": "WARNING", "themes": [{"name": NOV_NAME}]}]
    return json.dumps(out)


def test_matched_themes_finds_pass_among_two_checks():
    assert th.matched_pp_themes(_checks(1.12, themes_pass=True)) == [NOV_NAME]
    assert th.matched_pp_themes(_checks(1.12)) == []


def test_rule_match_before_description():
    t = th.parse_theme(NOV_NAME, "2026-10-26", "2026-11-29", GLB_RULE)
    assert th.rule_match(t, _checks(1.12), "GLB", 1)
    assert not th.rule_match(t, _checks(0.91), "GLB", 1)
    assert not th.rule_match(t, _checks(1.12), "USA", 1)
    assert not th.rule_match(t, _checks(1.12), "GLB", 0)
    assert th.theme_names(_checks(1.12), t, "GLB", 1) == [NOV_NAME]
    assert th.theme_names(_checks(1.12, themes_pass=True), t, "GLB", 1) == [NOV_NAME]


def test_verdict_uses_callers_theme_decision():
    chk = json.loads(_checks(1.12)) + [{"name": "POWER_POOL_CORRELATION", "result": "PASS", "value": 0.2}]
    assert "no Power Pool theme" in verdict(1.3, json.dumps(chk), require_theme=True)[1]
    assert verdict(1.3, json.dumps(chk), require_theme=True, theme_ok=True)[0] == "pp_ok"


ARTICLE = """Current month Power Pool Themes
October 31, 2026 17:00 Updated ~ minute read
November
Mo\tTu\tWe\tTh\tFr\tSa\tSu
26 Oct\t27 Oct\t28 Oct\t29 Oct\t30 Oct\t31 Oct\t1 Nov


GLB/D1 Power Pool Nov'26: region=GLB & delay=1 & AMER Sharpe > 1


2\t3\t4\t5\t6\t7\t8


GLB/D1 Power Pool Nov'26: region=GLB & delay=1 & AMER Sharpe > 1


23\t24\t25\t26\t27\t28\t29


 
30\t \t \t \t \t \t 
Was this article helpful?
"""


def test_article_calendar_parses_to_theme_spans():
    from ppminer.themes_fetch import merge, parse
    rows = parse(ARTICLE, 2026)
    assert rows == [{"name": "GLB/D1 Power Pool Nov`26", "start": "2026-10-26", "end": "2026-11-08",
                     "rule": "region=GLB & delay=1 & AMER Sharpe > 1"}]        # empty weeks end the theme
    old = [{"name": "GLB/D1 Power Pool Nov`26", "start": "2026-10-26", "end": "2026-11-29", "rule": "x"},
           {"name": "D1 Power Pool Oct`26", "start": "2026-09-28", "end": "2026-10-11", "rule": "delay=1"}]
    assert [r["end"] for r in merge(old, rows)] == ["2026-10-11", "2026-11-08"]   # past themes kept, current replaced


def test_fetched_themes_override_the_repository_file(tmp_path, monkeypatch):
    import json as _j
    from ppminer import theme as th
    repo, fetched = tmp_path / "themes.json", tmp_path / "fetched.json"
    repo.write_text(_j.dumps([{"name": "GLB/D1 Power Pool Nov`26", "start": "2026-10-26", "end": "2026-11-29", "rule": "region=GLB"},
                              {"name": "Old", "start": "2026-09-01", "end": "2026-09-30", "rule": "delay=1"}]))
    fetched.write_text(_j.dumps([{"name": "GLB/D1 Power Pool Nov'26", "start": "2026-10-26", "end": "2026-11-22",
                                  "rule": "region=GLB & delay=1 & AMER Sharpe > 1"}]))
    monkeypatch.setattr(th, "THEMES_PATH", str(repo))
    monkeypatch.setattr(th, "FETCHED_PATH", str(fetched))
    got = {t.name: t for t in th.all_themes()}
    assert set(got) == {"GLB/D1 Power Pool Nov'26", "Old"} and str(got["GLB/D1 Power Pool Nov'26"].end) == "2026-11-22"
    assert th._same_name("GLB/D1 Power Pool Nov'26", "GLB/D1 Power Pool Nov`26")


def test_theme_bar_lowers_the_quality_bar_for_theme_matches_only():
    from ppminer.verdict import quality_gate
    assert quality_gate(1.2, 0.81, 0.02, "[]") != []                     # omEAbKjn: below 1.5 / 1.0
    assert quality_gate(1.2, 0.81, 0.02, "[]", theme_match=True) == []
    assert quality_gate(1.17, 0.91, 0.02, "[]", theme_match=True) != []  # sharpe still under 1.2
    assert quality_gate(1.25, 0.9, 0.02, "[]", universe="TOP1000", theme_match=True) == []   # never raises a bar
