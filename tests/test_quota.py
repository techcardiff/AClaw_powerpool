import datetime as dt
import json
import sqlite3

from ppminer.quota import ny_date, phase_for, quota


def test_ny_date_uses_new_york_day():
    # 21:42 New York on the 23rd is already the 24th in Singapore, but BRAIN counts the 23rd
    assert ny_date("2026-10-23T21:42:06-04:00") == dt.date(2026, 10, 23)
    assert ny_date("2026-10-24T02:00:00+00:00") == dt.date(2026, 10, 23)


def test_phases():
    first = dt.date(2026, 10, 23)
    assert phase_for(None, first)[:2] == ("not started", 4)
    assert phase_for(first, first + dt.timedelta(days=13))[1] == 4
    assert phase_for(first, first + dt.timedelta(days=14))[1] == 2
    assert phase_for(first, first + dt.timedelta(days=28))[1] == 1
    assert phase_for(first, first + dt.timedelta(days=42)) == ("after week 6", 1, 10, True)


def test_quota_counts_power_pool_only():
    c = sqlite3.connect(":memory:")
    c.execute("create table account_submitted (account, alpha_id, expr, canon, stems, tags, sharpe, date_submitted)")
    pp, reg = json.dumps(["PowerPoolSelected"]), json.dumps([])
    rows = [("A", "a1", pp, "2026-10-23T09:43:10-04:00"), ("A", "a2", pp, "2026-10-24T01:18:00-04:00"),
            ("A", "r1", reg, "2026-10-24T03:00:00-04:00")]
    for acct, aid, tags, d in rows:
        c.execute("insert into account_submitted values (?,?,?,?,?,?,?,?)", (acct, aid, "", "", "", tags, 1.0, d))
    q = quota(c, "A", dt.date(2026, 10, 24))
    assert q.first_pp == dt.date(2026, 10, 23)
    assert (q.per_day, q.used_today, q.remaining, q.theme_required) == (4, 1, 3, False)
