"""/advise in the Telegram bot: what to do next for an account, across the campaigns we track.

1. facts(): computed from data/pp.db, data/autosubmit.json, data/themes.json and BRAIN's list of the
   account's submitted alphas (cached 3 h): session, today's submissions, Power Pool phase and theme,
   Osmosis and super-alpha scopes (10 submitted alphas in a region/delay), the queue per scope, mining.
2. rules(): plain suggestions from those facts, each with the bot command that acts on it.
3. Qwen3.8 (A6000, through the workstation's Qwen tunnel) picks the 1-3 that matter most today and says
   why. The checklist itself always comes from the rules: in the first try Qwen, asked for a whole plan,
   misread "10 hours with simulations" as "stopped 14 h ago" and invented a GLB submission. Qwen lines that
   name a command not in the checklist are dropped. AQP is left out until its rules are known (owner, 23 Nov).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
from collections import Counter

from . import autosubmit, llm
from .quota import ny_date, ny_now, quota
from .report import DB_PATH, ROOT

THEMES = os.path.join(ROOT, "data", "themes.json")
SCOPE_CACHE = os.path.join(ROOT, "data", "scope_counts.json")
SCOPE_TTL = 3 * 3600
OSMOSIS_MIN = 10          # a scope counts for Osmosis, and can hold a super alpha, from 10 submitted alphas

CAMPAIGNS = """Rules of the campaigns (use only these, never invent others):
- Daily limit (owner's rule): at most 1 regular-or-Power-Pool alpha and 1 super alpha per account per BRAIN day,
  the least production-correlated first. BRAIN's day ends at 13:00 CST. The scheduler submits; people don't.
- Power Pool: alphas with at most 8 operators, 3 fields, one data category, Power Pool correlation < 0.5.
  Per-day allowance by phase since the account's first Power Pool alpha: weeks 1-2 4, weeks 3-4 2, weeks 5-6 1,
  then 1 a day and 10 a month that must match BRAIN's current theme. Each theme names a region/delay and dates.
- Osmosis: a region/delay scope counts only once the account has 10 submitted alphas in it.
- Super alphas: combine the account's own submitted alphas of one region/delay; BRAIN needs at least 10 there.
- Low production correlation raises an alpha's value more than extra submissions do."""

COMMANDS = """Bot commands you may suggest (exact syntax):
/focus <account> <REGION>/<delay> [days]  — submit that scope first
/regions <account> <REGION> [...] | all     — where the account mines
/pause <account> [hours], /resume <account>
/pass <account>                              — run the submit pass now (same daily limit)
/queue <account>, /status, /login <account>"""


def _scope(settings) -> str | None:
    try:
        s = json.loads(settings) if isinstance(settings, str) else (settings or {})
    except ValueError:
        return None
    return f"{s.get('region')}/{s.get('delay')}" if s.get("region") else None


def scope_counts(account: str, brain=None, now=None) -> dict[str, int]:
    """Submitted alphas per region/delay from BRAIN (status ACTIVE), cached SCOPE_TTL."""
    now = now or time.time()
    try:
        with open(SCOPE_CACHE) as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    hit = cache.get(account)
    if hit and now - hit["at"] < SCOPE_TTL:
        return hit["counts"]
    if brain is None:
        from . import cookies as ck
        from .brain import Brain
        brain = Brain.from_cookie(ck.load_cookie(account))
    from .brain import BASE
    counts, off = Counter(), 0
    while True:
        j = brain._req("GET", f"{BASE}/users/self/alphas", params={"limit": 100, "offset": off, "status": "ACTIVE"}).json()
        for a in j.get("results", []):
            if a.get("type", "REGULAR") == "SUPER":
                continue
            sc = _scope(a.get("settings"))
            if sc:
                counts[sc] += 1
        off += 100
        if off >= j.get("count", 0):
            break
    cache[account] = {"at": now, "counts": dict(counts)}
    try:
        with open(SCOPE_CACHE, "w") as f:
            json.dump(cache, f)
    except OSError:
        pass
    return dict(counts)


def themes(today: dt.date) -> tuple[dict | None, dict | None]:
    """(current theme, next theme) from data/themes.json."""
    try:
        with open(THEMES) as f:
            ts = json.load(f)
    except (OSError, ValueError):
        return None, None
    cur = next((t for t in ts if t.get("start", "") <= str(today) <= t.get("end", "")), None)
    nxt = min((t for t in ts if t.get("start", "") > str(today)), key=lambda t: t["start"], default=None)
    return cur, nxt


def facts(conn, account: str, counts: dict[str, int], session_h=None, now=None) -> dict:
    now = now or ny_now()
    today = now.date()
    cfg = autosubmit.load_config(autosubmit.CONFIG)
    acfg = cfg.get("accounts", {}).get(account, {})
    end = dt.datetime.combine(today + dt.timedelta(days=1), dt.time(0), now.tzinfo)
    supers = {a for (a,) in conn.execute("select alpha_id from super_alphas where account=?", (account,))} \
        if conn.execute("select 1 from sqlite_master where name='super_alphas'").fetchone() else set()
    subs_today = [(aid, aid in supers) for aid, d in conn.execute(
        "select alpha_id, date_submitted from account_submitted where account=?", (account,)) if ny_date(d) == today]
    q = quota(conn, account, today)
    cur, nxt = themes(today)
    theme_scope = None
    if cur:
        rule = cur.get("rule", "")
        r = next((w.split("=")[1] for w in rule.replace(" ", "").split("&") if w.startswith("region=")), None)
        d = next((w.split("=")[1] for w in rule.replace(" ", "").split("&") if w.startswith("delay=")), None)
        theme_scope = f"{r}/{d}" if r and d else None
    picks = Counter(_scope(s) for (s,) in conn.execute(
        "select r.settings from picks p join resims r on r.id=p.resim_id where p.account=? and p.status in "
        "('proposed','approved')", (account,)))
    regular = Counter(_scope(s) for (s,) in conn.execute(
        "select settings from regular_queue where account=? and status='queued'", (account,)))
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    sims = conn.execute("select count(*), count(distinct strftime('%Y-%m-%d %H', created_at)) from resims "
                        "where account=? and created_at>=?", (account, since)).fetchone()
    sims_by_scope = Counter(_scope(s) for (s,) in conn.execute(
        "select settings from resims where account=? and created_at>=?", (account, since)))
    super_waiting = conn.execute("select count(*) from super_alphas where account=? and status='simulated'",
                                 (account,)).fetchone()[0] if supers or conn.execute(
        "select 1 from sqlite_master where name='super_alphas'").fetchone() else 0
    st = acfg.get("scope_targets") or {}
    return {
        "account": account,
        "now_cst": dt.datetime.now().strftime("%a %d %b %H:%M"),
        "brain_day_ends_in_h": round((end - now).total_seconds() / 3600, 1),
        "session_hours_left": None if session_h is None else round(session_h, 1),
        "submitted_today": {"regular_or_pp": sum(not s for _, s in subs_today), "super": sum(s for _, s in subs_today),
                            "limit": f"{acfg.get('daily_total', 1)} regular-or-PP + {1 if (acfg.get('super') or {}).get('enabled') else 0} super"},
        "power_pool": {"phase": q.phase, "per_day": q.per_day, "used_today": q.used_today,
                       "used_this_month": q.used_month, "month_cap": q.month_cap, "theme_required": q.theme_required},
        "theme_now": cur and {"name": cur["name"], "scope": theme_scope, "rule": cur.get("rule"), "ends": cur.get("end")},
        "theme_next": nxt and {"name": nxt["name"], "rule": nxt.get("rule"), "starts": nxt.get("start")},
        "submitted_per_scope": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "osmosis_scopes": sorted(s for s, n in counts.items() if n >= OSMOSIS_MIN),
        "near_10": {s: n for s, n in counts.items() if 5 <= n < OSMOSIS_MIN},
        "super_enabled": bool((acfg.get("super") or {}).get("enabled")),
        "super_variants_waiting": super_waiting,
        "queue_pp_picks_per_scope": {k: v for k, v in picks.items() if k},
        "queue_regular_per_scope": {k: v for k, v in regular.items() if k},
        "mining_regions": acfg.get("regions") or cfg.get("regions", ["USA"]),
        "focus": st.get("scopes") if st.get("until", "") >= str(today) else None,
        "sims_24h": sims[0], "hours_with_sims_24h": sims[1],
        "sims_24h_per_region": dict(Counter(_scope(x).split("/")[0] for (x,) in conn.execute(
            "select settings from resims where account=? and created_at>=?", (account, since)) if _scope(x))),
        "sims_24h_per_scope": {k: v for k, v in sims_by_scope.most_common(6) if k},
    }


def rules(f: dict) -> list[str]:
    """Plain suggestions, most urgent first."""
    a, out = f["account"], []
    if f["session_hours_left"] is None:
        out.append(f"🔴 No live session: nothing simulates or submits. /login {a} and do the face scan.")
    elif f["session_hours_left"] < 1:
        out.append(f"⏳ Session ends in {f['session_hours_left']}h; renew with /login {a} before it lapses.")
    if f["hours_with_sims_24h"] < 16:
        out.append(f"⏱ Simulated in only {f['hours_with_sims_24h']} of the last 24 h: renew the session as soon as the link comes.")
    t = f["theme_now"]
    if t and t["scope"]:
        r = t["scope"].split("/")[0]
        if f["queue_pp_picks_per_scope"].get(t["scope"], 0) == 0:
            out.append(f"🎯 Power Pool theme {t['name']} ({t['scope']}, until {t['ends']}) and no {t['scope']} picks queued. "
                       f"It is mined automatically; to put more into it: /regions {a} {r}.")
        if (f["focus"] or []) != [t["scope"]] and f["power_pool"]["used_today"] < f["power_pool"]["per_day"]:
            out.append(f"🎯 To submit theme alphas first: /focus {a} {t['scope']} {max(1, (dt.date.fromisoformat(t['ends']) - ny_now().date()).days + 1)}")
    for r in f["mining_regions"]:
        got, total = f["sims_24h_per_region"].get(r, 0), f["sims_24h"]
        if total >= 100 and got < 0.05 * total:
            out.append(f"🌍 {r} is a mining region but got {got} of {total} simulations in 24 h "
                       f"(the theme region and others took them); worth checking why.")
    for s in f["osmosis_scopes"]:
        if f["super_enabled"] and s != "USA/1" and f["submitted_per_scope"].get(s, 0) < OSMOSIS_MIN + 2:
            out.append(f"🧩 {s} just reached {f['submitted_per_scope'][s]} submitted alphas: super alphas are possible there "
                       f"now; the super lane tries its {s} variants again within a day.")
    for s, n in sorted(f["near_10"].items(), key=lambda kv: -kv[1]):
        out.append(f"🧪 {s} has {n} submitted alphas; {OSMOSIS_MIN - n} more make it count for Osmosis and allow super alphas there: /focus {a} {s}")
    if f["super_enabled"] and f["submitted_today"]["super"] == 0 and f["super_variants_waiting"]:
        out.append(f"🧩 {f['super_variants_waiting']} super variants waiting on BRAIN's check; the next pass tries them (/pass {a} to run it now).")
    if f["submitted_today"]["regular_or_pp"] == 0 and f["brain_day_ends_in_h"] < 4 and \
            (f["queue_pp_picks_per_scope"] or f["queue_regular_per_scope"]):
        out.append(f"📤 Nothing submitted today and the BRAIN day ends in {f['brain_day_ends_in_h']}h: /pass {a}")
    return out


def advise(conn, account: str, counts: dict[str, int], session_h=None, use_llm: bool = True) -> str:
    f = facts(conn, account, counts, session_h)
    sug = rules(f)
    head = f"🧭 {account} — what to do next"
    body = "\n".join(sug) or "✅ Nothing needs you right now: session live, theme and scopes covered."
    if not (use_llm and sug):
        return f"{head}\n{body}"
    if not llm.up():
        return f"{head}\n{body}\n(Qwen not reachable: no priorities added.)"
    try:
        text = llm.chat([
            {"role": "system", "content": "You advise a WorldQuant BRAIN consultant who runs an automatic alpha miner. "
             "From the checklist you are given, pick the 1 to 3 items that matter most today. For each, write one "
             "line starting with '→': your own sentence of at most 25 words saying what to do and why it matters "
             "more than the others today (don't copy the checklist line). Plain text. Use only the facts and rules "
             "given; quote numbers exactly as they appear; never invent alphas, times or commands. A command may "
             "appear only if it is in the checklist.\n\n" + CAMPAIGNS},
            {"role": "user", "content": "Facts (JSON):\n" + json.dumps(f, indent=1, default=str) +
             "\n\nChecklist:\n" + "\n".join(sug)}], max_tokens=300, temperature=0.1, retries=1, timeout=180)
    except llm.LLMError:
        return f"{head}\n{body}\n(Qwen didn't answer: no priorities added.)"
    allowed = {w for line in sug for w in line.split() if w.startswith("/")}
    keep = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("→")
            and all(w in allowed for w in ln.split() if w.startswith("/"))][:3]
    return f"{head}\n{body}" + ("\n\nQwen's priorities:\n" + "\n".join(keep) if keep else "")
