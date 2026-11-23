import json, sqlite3, time

import pytest

from ppminer import autosubmit, control, db, report, telegram as tgm


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "pp.db"
    c = sqlite3.connect(path)
    c.executescript(db.SCHEMA)
    c.execute("create table if not exists runner_state (key text primary key, value text, updated_at timestamp)")
    c.execute("create table if not exists regular_queue (account, alpha_id, settings, sharpe, fitness, status)")
    c.execute("insert into runner_state(key, value) values ('autosubmit:YO', ?)", (json.dumps("2026-11-23T04:38:07-05:00"),))
    c.execute("insert into regular_queue values ('YO', 'R1', ?, 2.1, 1.4, 'queued')", (json.dumps({"region": "GLB", "delay": 1}),))
    c.commit()
    c.close()
    cfg = tmp_path / "autosubmit.json"
    cfg.write_text(json.dumps({"regions": ["USA", "EUR"], "accounts": {
        "HT": {"enabled": True, "regions": ["EUR"]}, "YO": {"enabled": True, "daily_total": 1}}}))
    monkeypatch.setattr(control, "DB_PATH", str(path))
    monkeypatch.setattr(autosubmit, "CONFIG", str(cfg))
    sent = []
    tg = type("TG", (), {"send": lambda self, chat, text, button=None: sent.append((chat, text))})()
    b = tgm.Broker({"bot_token": "x", "admin_chat_ids": [1], "accounts": {"HT": {"chat_id": 1}, "YO": {"chat_id": 2}}}, tg=tg)
    monkeypatch.setattr(b, "session_left", lambda a: 3.0)
    return b, sent, path, cfg


def _state(path, key):
    row = sqlite3.connect(path).execute("select value from runner_state where key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def test_owner_steers_only_their_own_account_and_admin_hears_of_it(env):
    b, sent, path, cfg = env
    b.handle({"chat": {"id": 2}, "text": "/pause 3"})                      # YO's owner: account implied
    assert _state(path, "brain_pause:YO") > time.time() + 2.9 * 3600
    assert sent[0][0] == 2 and "paused" in sent[0][1] and sent[1] == (1, "(by YO's owner) " + sent[0][1])
    b.handle({"chat": {"id": 2}, "text": "/pause HT"})                      # not theirs
    assert _state(path, "brain_pause:HT") is None and "Which account?" in sent[-1][1]
    b.handle({"chat": {"id": 2}, "text": "/resume"})
    assert _state(path, "brain_pause:YO") is None


def test_regions_focus_and_pass_change_what_the_scheduler_reads(env):
    b, sent, path, cfg = env
    b.handle({"chat": {"id": 1}, "text": "/regions YO glb asi"})
    b.handle({"chat": {"id": 1}, "text": "/focus YO EUR/1 glb/1 5"})
    c = json.loads(cfg.read_text())["accounts"]["YO"]
    assert c["regions"] == ["GLB", "ASI"] and c["scope_targets"]["scopes"] == ["EUR/1", "GLB/1"]
    assert c["daily_total"] == 1                                             # untouched
    b.handle({"chat": {"id": 1}, "text": "/regions YO mars"})
    assert "Unknown region mars" in sent[-1][1] and json.loads(cfg.read_text())["accounts"]["YO"]["regions"] == ["GLB", "ASI"]
    b.handle({"chat": {"id": 1}, "text": "/regions YO all"})
    b.handle({"chat": {"id": 1}, "text": "/focus YO off"})
    c = json.loads(cfg.read_text())["accounts"]["YO"]
    assert "regions" not in c and "scope_targets" not in c
    b.handle({"chat": {"id": 1}, "text": "/pass YO"})
    assert _state(path, "autosubmit:YO") is None and "submit pass" in sent[-1][1]
    b.handle({"chat": {"id": 1}, "text": "/pass"})                           # admin with two accounts must name one
    assert "Which account?" in sent[-1][1]


def test_status_and_queue(env):
    b, sent, path, cfg = env
    b.handle({"chat": {"id": 1}, "text": "/status"})
    text = sent[-1][1]
    assert "HT  🟢 3.0h left" in text and "regions: EUR" in text and "regions: USA, EUR" in text
    assert "next about" in text
    b.handle({"chat": {"id": 2}, "text": "/queue"})
    assert "R1 GLB D1 Sharpe 2.1 fitness 1.4" in sent[-1][1]


def test_help_lists_the_commands(env):
    b, sent, *_ = env
    b.handle({"chat": {"id": 2}, "text": "/help"})
    assert "/pause" in sent[-1][1] and "/focus" in sent[-1][1]
