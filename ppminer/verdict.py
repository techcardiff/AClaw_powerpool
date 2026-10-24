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
