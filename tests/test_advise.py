import datetime as dt
import json
import sqlite3

import pytest

from ppminer import advise, autosubmit, db, llm, picks


@pytest.fixture
def conn(tmp_path, monkeypatch):
    c = sqlite3.connect(":memory:")
    c.executescript(db.SCHEMA)
    c.executescript(picks.SCHEMA if hasattr(picks, "SCHEMA") else "")
    for t in ("create table if not exists account_submitted (account, alpha_id, expr, canon, stems, tags, sharpe, date_submitted)",
              "create table if not exists regular_queue (account, alpha_id, settings, sharpe, fitness, status)",
              "create table if not exists super_alphas (account, alpha_id, settings, status, created_at)",
              "create table if not exists picks (alpha_id, resim_id, account, status, score)"):
        c.execute(t)
    c.execute("insert into super_alphas values ('HT', 'S1', '{}', 'simulated', '2026-11-23')")
    c.execute("insert into resims(id, account, alpha_id, settings, status, created_at) values (1, 'HT', 'P1', ?, 'done', datetime('now'))",
              (json.dumps({"region": "EUR", "delay": 1}),))
    c.execute("insert into picks(alpha_id, resim_id, account, status, score) values ('P1', 1, 'HT', 'proposed', 1.0)")
    cfg = tmp_path / "autosubmit.json"
    cfg.write_text(json.dumps({"accounts": {"HT": {"enabled": True, "daily_total": 1, "regions": ["EUR", "GLB"],
                                                   "super": {"enabled": True}}}}))
    th = tmp_path / "themes.json"
    today = advise.ny_now().date()
    th.write_text(json.dumps([{"name": "ASI/D1 Power Pool Dec`26", "start": str(today - dt.timedelta(days=1)),
                               "end": str(today + dt.timedelta(days=6)), "rule": "region=ASI & delay=1"}]))
    monkeypatch.setattr(autosubmit, "CONFIG", str(cfg))
    monkeypatch.setattr(advise, "THEMES", str(th))
    return c


COUNTS = {"USA/1": 160, "EUR/1": 9, "USA/0": 7, "GLB/1": 4}


def test_facts_and_rules_cover_theme_osmosis_super(conn):
    f = advise.facts(conn, "HT", COUNTS, session_h=3.0)
    assert f["theme_now"]["scope"] == "ASI/1" and f["osmosis_scopes"] == ["USA/1"]
    assert f["near_10"] == {"EUR/1": 9, "USA/0": 7} and f["queue_pp_picks_per_scope"] == {"EUR/1": 1}
    text = "\n".join(advise.rules(f))
    assert "no ASI/1 picks queued" in text and "/regions HT ASI" in text and "/focus HT ASI/1" in text
    assert "EUR/1 has 9 submitted alphas; 1 more" in text and "/focus HT EUR/1" in text
    assert "1 super variants waiting" in text


def test_no_session_comes_first_and_qwen_down_falls_back(conn, monkeypatch):
    monkeypatch.setattr(llm, "up", lambda: False)
    text = advise.advise(conn, "HT", COUNTS, session_h=None)
    lines = text.splitlines()
    assert lines[1].startswith("🔴 No live session") and "/login HT" in lines[1]
    assert "Qwen not reachable" in text


def test_qwen_only_ranks_the_checklist_and_invented_commands_are_dropped(conn, monkeypatch):
    seen = {}
    monkeypatch.setattr(llm, "up", lambda: True)
    reply = "→ EUR/1 first: one more alpha unlocks it. /focus HT EUR/1\n→ Pause it /pause HT\nnoise line"
    monkeypatch.setattr(llm, "chat", lambda msgs, **kw: seen.setdefault("m", msgs) and reply)
    text = advise.advise(conn, "HT", COUNTS, session_h=2.0)
    assert "EUR/1 has 9 submitted alphas" in text                       # checklist always there
    assert "Qwen's priorities:\n→ EUR/1 first: one more alpha unlocks it. /focus HT EUR/1" in text
    assert "Pause it" not in text and "noise" not in text             # /pause is never in the checklist
    assert '"EUR/1": 9' in seen["m"][1]["content"] and "Osmosis" in seen["m"][0]["content"]


def test_starved_region_and_new_super_scope(conn):
    f = advise.facts(conn, "HT", {"USA/1": 160, "EUR/1": 10}, session_h=3.0)
    f.update(sims_24h=1000, sims_24h_per_region={"EUR": 990, "ASI": 10})
    text = "\n".join(advise.rules(f))
    assert "GLB is a mining region but got 0 of 1000" in text
    assert "EUR/1 just reached 10 submitted alphas: super alphas are possible" in text


def test_scope_counts_skip_super_alphas_and_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(advise, "SCOPE_CACHE", str(tmp_path / "sc.json"))
    calls = []

    class B:
        def _req(self, m, url, params=None):
            calls.append(params["offset"])
            res = [{"type": "REGULAR", "settings": {"region": "EUR", "delay": 1}}] * 2 + \
                  [{"type": "SUPER", "settings": {"region": "USA", "delay": 1}}]
            return type("R", (), {"json": lambda s: {"count": 3, "results": res}})()

    assert advise.scope_counts("HT", B(), now=1000) == {"EUR/1": 2}
    assert advise.scope_counts("HT", B(), now=2000) == {"EUR/1": 2} and calls == [0]
