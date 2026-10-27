"""Power Pool verdict from BRAIN's own /check output (same logic as the dashboard)."""
from __future__ import annotations

import json


def verdict(sharpe: float | None, checks_json: str | None, require_theme: bool = False) -> tuple[str, list[str]]:
    """Returns (status, reasons): status is pp_ok | risky | pending | not_pp.
    require_theme: pure Power Pool alphas only need a theme match after week 6 of the
    account's first Power Pool submission (and for the thematic leaderboard)."""
    checks = {c.get("name"): c for c in json.loads(checks_json or "[]") or []}
    res = lambda n: (checks.get(n) or {}).get("result")
    reasons = []
    if (sharpe or 0) < 1:
        reasons.append("sharpe < 1")
    for n in ("LOW_TURNOVER", "HIGH_TURNOVER"):
        if res(n) == "FAIL":
            reasons.append(n.lower())
    ppc = checks.get("POWER_POOL_CORRELATION") or {}
    if ppc.get("result") in ("FAIL", "WARNING"):
        # WARNING = above 0.5 with an alpha already in the Power Pool; only eligible with
        # sharpe >= 10% above that alpha, which BRAIN decides at submission
        reasons.append(f"PP correlation {ppc.get('value', '?')} > {ppc.get('limit', 0.5)}")
    themes = [t["name"] for t in (checks.get("MATCHES_THEMES") or {}).get("themes", []) if "power pool" in t["name"].lower()]
    if not themes and require_theme:
        reasons.append("no Power Pool theme")
    if not ppc and res("CONCENTRATED_WEIGHT") == "FAIL":
        # BRAIN never computes the correlation checks for these, they stay PENDING forever
        reasons.append("concentrated weight (BRAIN skips its correlation checks)")
    if reasons:
        return "not_pp", reasons
    if res("POWER_POOL_CORRELATION") != "PASS":
        if res("REGULAR_SUBMISSION") == "FAIL":
            # daily submission limit reached: BRAIN skips the correlation checks until the next day (New York)
            return "pending", ["daily submission limit reached; correlation computes after midnight New York"]
        return "pending", ["PP correlation not computed yet (run recheck)"]
    # not Power Pool rules, but signs of a fragile signal we should not submit
    risk = []
    if res("CONCENTRATED_WEIGHT") == "FAIL":
        risk.append("concentrated weight")
    two_y = (checks.get("LOW_2Y_SHARPE") or {}).get("value")
    if two_y is not None and two_y < 0.5:
        risk.append(f"last-2y sharpe {two_y}")
    sub = (checks.get("LOW_SUB_UNIVERSE_SHARPE") or {}).get("value")
    if sub is not None and sub < 0:
        risk.append(f"sub-universe sharpe {sub}")
    return ("risky", risk) if risk else ("pp_ok", [])


# ── Lane C: regular alphas ──────────────────────────────────────────────────
# Regular submission needs every check to PASS; these are the ones settings tuning
# can plausibly move (decay/truncation/neutralization/universe).
TUNABLE = {"LOW_SHARPE", "LOW_FITNESS", "CONCENTRATED_WEIGHT", "LOW_SUB_UNIVERSE_SHARPE", "HIGH_TURNOVER", "LOW_TURNOVER"}
SELF_CORR_GIVE_UP = 0.8      # tuning settings rarely moves self-correlation by more than ~0.1


def regular_verdict(checks_json: str | None) -> tuple[str, list[str]]:
    """Returns (status, failing): status is reg_ok | pending | near | fail.
    near = only tunable checks fail, by a margin worth a settings sweep."""
    checks = {c.get("name"): c for c in json.loads(checks_json or "[]") or []}
    # REGULAR_SUBMISSION is the daily submission quota, not a property of the alpha
    fails = [n for n, c in checks.items() if c.get("result") == "FAIL" and n not in ("MATCHES_THEMES", "REGULAR_SUBMISSION")]
    pending = [n for n, c in checks.items() if c.get("result") == "PENDING"]
    if not fails:
        return ("pending", pending) if pending else ("reg_ok", [])
    sc = checks.get("SELF_CORRELATION") or {}
    if set(fails) - TUNABLE - {"SELF_CORRELATION", "LOW_2Y_SHARPE"}:
        return "fail", fails
    two_y = checks.get("LOW_2Y_SHARPE") or {}
    if two_y.get("result") == "FAIL" and (two_y.get("value") or 0) < 1.0:
        return "fail", fails             # the edge has faded recently; settings won't bring it back
    if "SELF_CORRELATION" in fails and (sc.get("value") or 1) >= SELF_CORR_GIVE_UP:
        return "fail", fails
    ls = checks.get("LOW_SHARPE") or {}
    if ls.get("result") == "FAIL" and (ls.get("value") or 0) < 0.8 * (ls.get("limit") or 1.58):
        return "fail", fails
    lf = checks.get("LOW_FITNESS") or {}
    if lf.get("result") == "FAIL" and (lf.get("value") or 0) < 0.6 * (lf.get("limit") or 1.0):
        return "fail", fails
    return "near", fails


# ── ATOM ────────────────────────────────────────────────────────────────────
# ATOM: an alpha on a single dataset that passes every test except the IS ladder.
# Like regular alphas it sits outside the pure Power Pool quota (the 4-per-day
# overall submission limit still applies).
NOT_QUALITY = {"MATCHES_THEMES", "REGULAR_SUBMISSION", "OSMOSIS_ALLOCATION"}


def atom_verdict(checks_json: str | None, single_dataset: bool) -> tuple[str, list[str]]:
    """Returns (status, failing): atom_ok | pending | not_atom."""
    if not single_dataset:
        return "not_atom", ["more than one dataset"]
    checks = {c.get("name"): c for c in json.loads(checks_json or "[]") or []}
    fails = [n for n, c in checks.items() if c.get("result") == "FAIL"
             and n not in NOT_QUALITY and "LADDER" not in n]
    if fails:
        return "not_atom", fails
    pending = [n for n, c in checks.items() if c.get("result") == "PENDING" and n not in NOT_QUALITY]
    return ("pending", pending) if pending else ("atom_ok", [])


# ── quality bar for automatic submission ────────────────────────────────────
# Power Pool income follows the quality of what is submitted, and the pool pays an
# alpha-count penalty, so auto-submit leaves slots empty rather than fill them with
# alphas that only just clear the 1.0 Sharpe rule. A value BRAIN did not report never blocks.
QUALITY_DEFAULTS = {
    "min_sharpe": 1.5,                 # Power Pool rule is 1.0
    "min_fitness": 1.0,                # sharpe x sqrt(returns / turnover): after-cost quality
    "max_turnover": 0.4,               # trading cost
    "min_2y_sharpe": 1.0,              # the edge is still alive recently
    "min_sub_universe_sharpe": 0.7,    # not only the most liquid names
    "concentrated_weight": True,       # CONCENTRATED_WEIGHT must not FAIL or WARN
    "max_fitness": 6.0,                # beyond this the result is implausible (near-zero divisors, few names)
    "max_returns": 1.0,                # annual return; 301% with 50% in one stock was a degenerate alpha
}


def quality_gate(sharpe, fitness, turnover, checks_json: str | None, bar: dict | None = None,
                 returns: float | None = None) -> list[str]:
    """Reasons an alpha falls below the auto-submit quality bar ([] = passes)."""
    b = dict(QUALITY_DEFAULTS, **(bar or {}))
    checks = {c.get("name"): c for c in json.loads(checks_json or "[]") or []}
    val = lambda n: (checks.get(n) or {}).get("value")
    out = []
    if sharpe is not None and sharpe < b["min_sharpe"]:
        out.append(f"sharpe {sharpe} < {b['min_sharpe']}")
    if fitness is not None and fitness < b["min_fitness"]:
        out.append(f"fitness {fitness} < {b['min_fitness']}")
    if turnover is not None and turnover > b["max_turnover"]:
        out.append(f"turnover {turnover} > {b['max_turnover']}")
    if (v := val("LOW_2Y_SHARPE")) is not None and v < b["min_2y_sharpe"]:
        out.append(f"last-2y sharpe {v} < {b['min_2y_sharpe']}")
    if (v := val("LOW_SUB_UNIVERSE_SHARPE")) is not None and v < b["min_sub_universe_sharpe"]:
        out.append(f"sub-universe sharpe {v} < {b['min_sub_universe_sharpe']}")
    if b["concentrated_weight"] and (checks.get("CONCENTRATED_WEIGHT") or {}).get("result") in ("FAIL", "WARNING"):
        out.append(f"concentrated weight ({(checks.get('CONCENTRATED_WEIGHT') or {}).get('value')})")
    if fitness is not None and fitness > b["max_fitness"]:
        out.append(f"fitness {fitness} > {b['max_fitness']} (implausible)")
    if returns is not None and returns > b["max_returns"]:
        out.append(f"returns {returns} > {b['max_returns']} (implausible)")
    return out
