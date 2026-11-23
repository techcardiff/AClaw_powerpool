import datetime as dt
import json
import sqlite3

from ppminer import autosubmit, db, report, telegram as tgm
from ppminer.quota import NY

DAY = dt.date(2026, 11, 22)


def _conn():
    c = sqlite3.connect(":memory:")
    c.executescript(db.SCHEMA)
    c.executescript(autosubmit.SCHEMA)
    for t in ("create table if not exists account_submitted (account, alpha_id, expr, canon, stems, tags, sharpe, date_submitted)",
              "create table if not exists regular_queue (account, alpha_id, settings, status)",
              "create table if not exists super_alphas (account, alpha_id, settings, status, created_at)"):
        c.execute(t)
    # submitted 06:44 New York on the 22nd; a 13:30 CST submission on the 22nd belongs to the 22nd too
    c.execute("insert into account_submitted values ('HT', 'PP1', '', '', '', ?, 1.9, '2026-11-22T06:44:52-05:00')",
              (json.dumps(["PowerPoolSelected"]),))
    c.execute("insert into account_submitted values ('HT', 'OLD', '', '', '', '[]', 2.0, '2026-11-21T23:59:00-05:00')")
    c.execute("insert into autosubmit_log(account, ny_date, alpha_id, action, detail) values "
              "('HT', '2026-11-22', 'PP1', 'submitted', 'sharpe 1.9 TO 0.31 prod 0.589 at x | least correlated of 3')")
    c.execute("insert into resims(account, alpha_id, settings, sharpe, status, created_at) values "
              "('HT', 'PP1', ?, 1.9, 'done', '2026-11-21 12:00:00')", (json.dumps({"region": "GLB", "delay": 1}),))
    for i, (st, sh, ts) in enumerate((("done", 2.4, "2026-11-22 06:00:00"), ("done", 4.1, "2026-11-23 04:59:00"),
                                      ("error", None, "2026-11-22 10:00:00"), ("done", 9.0, "2026-11-23 05:01:00"))):
        c.execute("insert into resims(account, alpha_id, sharpe, status, created_at) values ('HT', ?, ?, ?, ?)",
                  (f"x{i}", sh, st, ts))
    c.execute("insert into autosubmit_log(account, ny_date, action, detail) values "
              "('YO', '2026-11-22', 'daily_candidates', 'A1(pp) 0.91')")
    for m in range(90):                                  # 90 distinct minutes logged out
        c.execute("insert into autosubmit_log(ts, account, ny_date, action) values (?, 'YO', '2026-11-22', 'waiting_login')",
                  (f"2026-11-22 {10 + m // 60:02d}:{m % 60:02d}:00",))
    return c


def test_report_lists_the_days_submissions_and_simulations():
    text = report.build(_conn(), ["HT", "YO"], DAY, session=lambda a: 3.25 if a == "HT" else None)
    assert "✅ PP1: Power Pool · GLB D1 · Sharpe 1.9 · prod corr 0.589" in text
    assert "OLD" not in text                                       # the day before, New York time
    assert "2 simulations done, 1 errors, best Sharpe 4.10" in text  # the 05:01 UTC one is the next NY day
    assert "🟢 live, 3.2h left" in text and "🔴 logged out now" in text
    assert "➖ nothing submitted: 1 candidate(s) passed" in text
    assert "logged out about 1.5h" in text


def test_due_day_turns_over_just_after_new_york_midnight():
    assert report.due_day(dt.datetime(2026, 11, 23, 0, 4, tzinfo=NY)) == dt.date(2026, 11, 21)
    assert report.due_day(dt.datetime(2026, 11, 23, 0, 6, tzinfo=NY)) == DAY


def test_broker_sends_once_per_day_to_report_chats(monkeypatch, tmp_path):
    monkeypatch.setattr(report, "SENT_FILE", str(tmp_path / "sent"))
    monkeypatch.setattr(report, "connect", _conn)
    monkeypatch.setattr(report, "due_day", lambda now=None: DAY)
    sent = []
    tg = type("TG", (), {"send": lambda self, chat, text, button=None: sent.append((chat, text))})()
    cfg = {"bot_token": "x", "admin_chat_ids": [1], "report": {"chat_ids": [-1001]},
           "accounts": {"HT": {"chat_id": 1}, "YO": {"chat_id": 2}}}
    b = tgm.Broker(cfg, tg=tg)
    monkeypatch.setattr(b, "session_left", lambda a: None)
    assert b.maybe_report() and not b.maybe_report()
    assert [c for c, _ in sent] == [-1001] and "PP1" in sent[0][1]
    # /report from an admin answers on demand; an owner who isn't admin just gets the command list
    b.handle({"chat": {"id": 2}, "text": "/report"})
    b.handle({"chat": {"id": 1}, "text": "/report"})
    assert [c for c, _ in sent] == [-1001, 2, 1]
    assert sent[1][1].startswith("Commands") and "PP1" in sent[2][1]
