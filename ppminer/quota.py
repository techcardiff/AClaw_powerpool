"""Power Pool quota calendar per account, counted in New York days.

From BRAIN's Power Pool rules (quota update of 22 Sep 2026), by time since the
account's first Power Pool submission:
  weeks 1-2  4 pure Power Pool alphas per day
  weeks 3-4  2 per day
  weeks 5-6  1 per day
  after      1 per day, 10 per calendar month, and each must match an active theme
BRAIN's REGULAR_SUBMISSION check also caps an account at 4 submissions per day
in total (Power Pool submissions count toward it).
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from dataclasses import dataclass
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
DAILY_SUBMISSION_LIMIT = 4


def ny_now() -> dt.datetime:
    return dt.datetime.now(NY)


def ny_date(ts: str | None) -> dt.date | None:
    """BRAIN timestamps look like 2026-10-23T09:43:10-04:00."""
    if not ts:
        return None
    return dt.datetime.fromisoformat(ts).astimezone(NY).date()


@dataclass
class Quota:
    account: str
    today: dt.date
    first_pp: dt.date | None
    phase: str
    per_day: int
    used_today: int
    used_month: int
    month_cap: int | None
    theme_required: bool

    @property
    def remaining(self) -> int:
        left = self.per_day - self.used_today
        if self.month_cap is not None:
            left = min(left, self.month_cap - self.used_month)
        return max(0, left)


def phase_for(first_pp: dt.date | None, today: dt.date) -> tuple[str, int, int | None, bool]:
    """(phase, per_day, month_cap, theme_required)."""
    if first_pp is None:
        return "not started", 4, None, False
    days = (today - first_pp).days
    if days < 14:
        return "weeks 1-2", 4, None, False
    if days < 28:
        return "weeks 3-4", 2, None, False
    if days < 42:
        return "weeks 5-6", 1, None, False
    return "after week 6", 1, 10, True


def quota(conn: sqlite3.Connection, account: str, today: dt.date | None = None) -> Quota:
    """Needs account_submitted to be fresh (lanes.sync_submitted)."""
    today = today or ny_now().date()
    pp_dates = []
    for tags, date in conn.execute("select tags, date_submitted from account_submitted where account=?", (account,)):
        if "PowerPoolSelected" in json.loads(tags or "[]") and ny_date(date):
            pp_dates.append(ny_date(date))
    first = min(pp_dates) if pp_dates else None
    phase, per_day, cap, theme = phase_for(first, today)
    return Quota(account, today, first, phase, per_day,
                 used_today=sum(d == today for d in pp_dates),
                 used_month=sum(d.year == today.year and d.month == today.month for d in pp_dates),
                 month_cap=cap, theme_required=theme)
