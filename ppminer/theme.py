"""Power Pool themes: parse the theme strings BRAIN publishes and match alphas.

Example theme string (Oct 2026):
    delay=1 & datacategory in ['analyst', 'model', 'news'] & single datacategory
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field

# BRAIN's own calendar had a typo ('fundamnetal'); accept common misspellings.
_ALIASES = {"fundamnetal": "fundamental", "fundamentals": "fundamental", "price volume": "pv",
            "pricevolume": "pv", "price_volume": "pv"}


@dataclass
class Theme:
    name: str
    start: dt.date
    end: dt.date                      # inclusive
    delay: int | None = None
    categories: set[str] = field(default_factory=set)
    single_category: bool = False
    regions: set[str] = field(default_factory=set)   # empty = any
    raw: str = ""

    def active_on(self, day: dt.date) -> bool:
        return self.start <= day <= self.end

    def matches(self, *, delay: int, region: str | None, categories: set[str]) -> bool:
        if self.delay is not None and delay != self.delay:
            return False
        if self.regions and region and region not in self.regions:
            return False
        if self.single_category and len(categories) != 1:
            return False
        if self.categories and not categories <= self.categories:
            return False
        return True


def parse_theme(name: str, start: str, end: str, text: str) -> Theme:
    t = Theme(name=name, start=dt.date.fromisoformat(start), end=dt.date.fromisoformat(end), raw=text)
    low = text.lower()
    if m := re.search(r"delay\s*=\s*(\d)", low):
        t.delay = int(m.group(1))
    if m := re.search(r"datacategory\s+in\s*\[([^\]]*)\]", low):
        cats = [c.strip().strip("'\"") for c in m.group(1).split(",") if c.strip()]
        t.categories = {_ALIASES.get(c, c) for c in cats}
    if m := re.search(r"region\s+in\s*\[([^\]]*)\]", low):
        t.regions = {c.strip().strip("'\"").upper() for c in m.group(1).split(",") if c.strip()}
    elif m := re.search(r"region\s*=\s*'?(\w+)", low):
        t.regions = {m.group(1).upper()}
    t.single_category = "single datacategory" in low or "single data category" in low
    return t


def load_themes(path: str) -> list[Theme]:
    with open(path) as f:
        rows = json.load(f)
    return [parse_theme(r["name"], r["start"], r["end"], r["rule"]) for r in rows]


def active_themes(themes: list[Theme], day: dt.date | None = None) -> list[Theme]:
    day = day or dt.date.today()
    return [t for t in themes if t.active_on(day)]
