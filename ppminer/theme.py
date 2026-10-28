"""Power Pool themes: parse the theme strings BRAIN publishes and match alphas.

Example theme string (Oct 2026):
    delay=1 & datacategory in ['analyst', 'model', 'news'] & single datacategory

BRAIN's MATCHES_THEMES check names the themes an alpha could count toward: with
result PASS they are the themes it matches, with WARNING the ones it does not. It only
reports a Power Pool theme as matched once the alpha has a description (28 Oct: HT52122's
3qorW3L6, AMER Sharpe 1.12, moved from WARNING to PASS when one was set), and simulation
results never have one. So before submission an alpha is judged by the theme's own rule
(rule_match); auto-submit sets the description first and then BRAIN's check confirms it.
"""
from __future__ import annotations

import datetime as dt
import json
import os
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
    # with a match BRAIN lists two MATCHES_THEMES checks: PASS (matched) and WARNING (the rest)
    return [t["name"] for c in checks if c.get("name") == "MATCHES_THEMES" and c.get("result") == "PASS"
            for t in c.get("themes", []) if "power pool" in t["name"].lower()]


def rule_match(theme: Theme | None, checks, region: str | None, delay: int | None,
               categories: set[str] | None = None) -> bool:
    """The theme's own rule on a result: region, delay, data categories (when known) and
    sub-region Sharpes ('AMER Sharpe > 1': BRAIN's LOW_GLB_AMER_SHARPE value)."""
    if theme is None or (theme.delay is not None and delay is not None and delay != theme.delay):
        return False
    if theme.regions and region not in theme.regions:
        return False
    if categories is not None and not theme.matches(delay=theme.delay or 1, region=region, categories=categories):
        return False
    have = sub_sharpes(checks)
    return all((have.get(part) or 0) > need for part, need in theme.sub_sharpe.items())


def theme_names(checks, theme: Theme | None, region: str | None, delay: int | None,
                categories: set[str] | None = None) -> list[str]:
    """Power Pool themes a result matches: BRAIN's word when it has one (described alphas),
    otherwise the active theme's rule."""
    names = matched_pp_themes(checks)
    if theme is not None and theme.name not in names and rule_match(theme, checks, region, delay, categories):
        names.append(theme.name)
    return names


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


THEMES_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "themes.json")


def current(conn) -> Theme | None:
    """detect() with themes.json, as the scheduler steers by it."""
    return detect(conn, load_themes(THEMES_PATH) if os.path.exists(THEMES_PATH) else [])
