"""Power Pool themes: parse the theme strings BRAIN publishes and match alphas.

Example theme string (Oct 2026):
    delay=1 & datacategory in ['analyst', 'model', 'news'] & single datacategory

BRAIN's MATCHES_THEMES check names the themes an alpha could count toward: with
result PASS they are the themes it matches, with WARNING the ones it does not.
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
    sub_sharpe: dict[str, float] = field(default_factory=dict)   # GLB part -> minimum Sharpe, e.g. {"AMER": 1.0}
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
    # 'AMER Sharpe > 1': the Sharpe of the alpha's Americas part of GLB (BRAIN check LOW_GLB_AMER_SHARPE)
    for part, x in re.findall(r"\b(amer|emea|apac)\s+sharpe\s*>=?\s*([\d.]+)", low):
        t.sub_sharpe[part.upper()] = float(x)
    return t


def load_themes(path: str) -> list[Theme]:
    with open(path) as f:
        rows = json.load(f)
    return [parse_theme(r["name"], r["start"], r["end"], r["rule"]) for r in rows]


def active_themes(themes: list[Theme], day: dt.date | None = None) -> list[Theme]:
    day = day or dt.date.today()
    return [t for t in themes if t.active_on(day)]


def sub_sharpes(checks) -> dict[str, float]:
    """GLB sub-region Sharpes from BRAIN's checks (LOW_GLB_AMER_SHARPE etc.): {"AMER": 0.58, ...}."""
    if isinstance(checks, str) or checks is None:
        checks = json.loads(checks or "[]") or []
    out = {}
    for c in checks:
        m = re.fullmatch(r"LOW_GLB_([A-Z]+)_SHARPE", c.get("name", ""))
        if m and c.get("value") is not None:
            out[m.group(1)] = c["value"]
    return out


def matched_pp_themes(checks) -> list[str]:
    """Power Pool themes this alpha matches, as BRAIN's MATCHES_THEMES check reports them
    (a list of check dicts, or its JSON). WARNING lists the themes it does NOT match."""
    if isinstance(checks, str) or checks is None:
        checks = json.loads(checks or "[]") or []
    mt = next((c for c in checks if c.get("name") == "MATCHES_THEMES"), {})
    if mt.get("result") != "PASS":
        return []
    return [t["name"] for t in mt.get("themes", []) if "power pool" in t["name"].lower()]


def from_name(name: str, day: dt.date | None = None) -> Theme:
    """Rule from a theme name such as 'GLB/D1 Power Pool Nov`26': region(s) and delay."""
    day = day or dt.date.today()
    t = Theme(name=name, start=day, end=day, raw=name)
    if m := re.match(r"\s*((?:[A-Z]{3}/)*)D(\d)\b", name):
        t.regions = {r for r in m.group(1).split("/") if r}
        t.delay = int(m.group(2))
    return t


def detect(conn, themes: list[Theme] = (), recent: int = 300) -> Theme | None:
    """The Power Pool theme BRAIN lists right now, read from the newest MATCHES_THEMES
    checks (every check names the active Power Pool theme, matched or not), so steering
    follows the weekly change without editing themes.json. A themes.json entry of the
    same name, or an entry active today, supplies categories the name doesn't carry."""
    name = None
    for (checks,) in conn.execute("""select checks from resims where status='done' and checks like '%MATCHES_THEMES%'
                                     order by id desc limit ?""", (recent,)):
        mt = next((c for c in json.loads(checks or "[]") or [] if c.get("name") == "MATCHES_THEMES"), {})
        name = next((t["name"] for t in mt.get("themes", []) if "power pool" in t["name"].lower()), None)
        if name:
            break
    if name:
        for t in themes:
            if t.name == name:
                return t
        t = from_name(name)
        if t.regions or t.categories:
            return t
    act = active_themes(list(themes))
    return act[0] if act else None
