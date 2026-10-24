"""Work lanes for the continuous runner.

Lane A  general Power Pool inventory (no theme in weeks 1-6): harvested PP-shaped
        alphas, Qwen rewrites and explore ideas, plus Qwen revisions of explore
        ideas that showed some signal (see revise.py).
Lane C  regular alphas in spare capacity: harvested alphas whose old simulation
        already looked regular-grade (any shape), and settings sweeps for near misses.

Both lanes skip field families that are known to collide: families folded into an
alpha already submitted (Power Pool correlation) and families of the account's own
submitted alphas (self-correlation).
"""
from __future__ import annotations

import json
import re
import sqlite3

from .brain import BASE, Brain
from .expr import GROUPING_FIELDS, analyze
from .harvest import SUSPICIOUS_SHARPE
from .select import _stems, shortlist
from .verdict import regular_verdict, verdict

MIGRATIONS = [
    "alter table resims add column lane text",
    "alter table resims add column purpose text",
    "alter table resims add column parent_resim_id integer",
    "alter table candidates add column parent_id integer",
    "alter table candidates add column depth integer default 0",
    """create table if not exists account_submitted (
        account text, alpha_id text, expr text, canon text, stems text, tags text,
        sharpe real, date_submitted text, primary key (account, alpha_id))""",
    "create table if not exists runner_state (key text primary key, value text, updated_at timestamp default current_timestamp)",
]


def migrate(conn: sqlite3.Connection) -> None:
    for sql in MIGRATIONS:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass                                   # column already there
    conn.commit()


def set_state(conn: sqlite3.Connection, **kv) -> None:
    for k, v in kv.items():
        conn.execute("insert into runner_state(key, value) values (?,?) on conflict(key) do update set "
                     "value=excluded.value, updated_at=current_timestamp", (k, json.dumps(v, default=str)))
    conn.commit()


# ── what the account already has ────────────────────────────────────────────
def field_stems(expr: str, catalog, region: str | None) -> str:
    """Field-family signature of a submitted expression. Comments are stripped
    (old studio alphas carry /* HYPOTHESIS ... */ blocks the parser rejects) and
    grouping fields (industry, subindustry, ...) are not data fields."""
    clean = re.sub(r"/\*.*?\*/", " ", expr, flags=re.S)
    clean = re.sub(r"(#|//)[^\n]*", " ", clean)
    s = analyze(clean)
    if s.ok:
        names = s.names
    else:   # still unparseable: fall back to plain identifiers
        names = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", clean)) - set(re.findall(r"([A-Za-z_]\w*)\s*\(", clean))
    return _stems(sorted(x for x in names if x not in GROUPING_FIELDS and catalog.get(x, region)))


def restem(conn: sqlite3.Connection, catalog) -> int:
    """Recompute stored field families (after a change to field_stems)."""
    rows = conn.execute("select account, alpha_id, expr from account_submitted").fetchall()
    for acct, aid, expr in rows:
        conn.execute("update account_submitted set stems=? where account=? and alpha_id=?",
                     (field_stems(expr, catalog, "USA"), acct, aid))
    conn.commit()
    return len(rows)



def sync_submitted(conn: sqlite3.Connection, brain: Brain, account: str, catalog) -> int:
    migrate(conn)
    off, n = 0, 0
    while True:
        j = brain._req("GET", f"{BASE}/users/self/alphas", params={"limit": 100, "offset": off, "status": "ACTIVE"}).json()
        for a in j.get("results", []):
            expr = (a.get("regular") or {}).get("code") or ""
            region = (a.get("settings") or {}).get("region")
            conn.execute("""insert or replace into account_submitted values (?,?,?,?,?,?,?,?)""",
                         (account, a["id"], expr, expr.replace(" ", ""), field_stems(expr, catalog, region),
                          json.dumps(a.get("tags")), (a.get("is") or {}).get("sharpe"), a.get("dateSubmitted")))
            n += 1
        off += 100
        if off >= j.get("count", 0):
            break
    conn.commit()
    return n


def collide_stems(conn: sqlite3.Connection, account: str) -> tuple[set[str], set[str]]:
    """(power_pool, self) field-family signatures that would fail correlation again."""
    migrate(conn)
    pp = set()
    rows = conn.execute("""select c.fields, r.checks from resims r join candidates c on c.id = r.candidate_id
                           where r.account=? and r.status='done'""", (account,)).fetchall()
    for fields, checks in rows:
        ch = {x.get("name"): x for x in json.loads(checks or "[]") or []}
        if (ch.get("POWER_POOL_CORRELATION") or {}).get("result") in ("FAIL", "WARNING"):
            pp.add(_stems(json.loads(fields)))
    for (members,) in conn.execute("select members from picks where account=? and status='submitted'", (account,)):
        for aid, _ in json.loads(members or "[]"):
            r = conn.execute("""select c.fields from resims r join candidates c on c.id=r.candidate_id
                                where r.alpha_id=?""", (aid,)).fetchone()
            if r:
                pp.add(_stems(json.loads(r[0])))
    own = {s for (s,) in conn.execute("select stems from account_submitted where account=?", (account,)) if s}
    return pp, own


def submitted_exprs(conn: sqlite3.Connection, account: str) -> set[str]:
    return {c for (c,) in conn.execute("select canon from account_submitted where account=?", (account,))}


# ── Lane A ──────────────────────────────────────────────────────────────────
LANE_A_STATUSES = ("pp_ready", "pp_multi", "rewrite", "explore")


def other_accounts(conn: sqlite3.Connection, account: str, others: list[str]) -> tuple[set[int], set[str], set[str]]:
    """What other accounts already used: candidate ids they simulated, field families they
    simulated or submitted, and their submitted expressions. Each consultant account's
    alphas must be its own research, so lanes never hand these to `account`."""
    ids, stems, exprs = set(), set(), set()
    for o in others:
        if o == account:
            continue
        for cid, fields in conn.execute("""select r.candidate_id, c.fields from resims r join candidates c
                                           on c.id = r.candidate_id where r.account=?""", (o,)):
            ids.add(cid)
            stems.add(_stems(json.loads(fields)))
        stems |= {s for (s,) in conn.execute("select stems from account_submitted where account=?", (o,)) if s}
        exprs |= submitted_exprs(conn, o)
    return ids, stems, exprs


def _without(rows, ids: set[int], n: int) -> list[sqlite3.Row]:
    return [r for r in rows if r["id"] not in ids][:n]


def lane_a(conn: sqlite3.Connection, account: str, n: int, others: list[str] = ()) -> list[sqlite3.Row]:
    pp, _ = collide_stems(conn, account)
    o_ids, o_stems, o_exprs = other_accounts(conn, account, list(others))
    # exact copies of already-submitted alphas are better tagged into the Power Pool than re-simulated
    rows = shortlist(conn, None, n=n * 3 if o_ids else n, statuses=LANE_A_STATUSES, account=account, skip_done=True,
                     exclude_stems=pp | o_stems, exclude_exprs=submitted_exprs(conn, account) | o_exprs,
                     max_per_dataset=3, max_sharpe=SUSPICIOUS_SHARPE)
    return _without(rows, o_ids, n)


# ── Lane C ──────────────────────────────────────────────────────────────────
REGULAR_SHARPE, REGULAR_FITNESS = 1.58, 1.0


def lane_c(conn: sqlite3.Connection, account: str, n: int, others: list[str] = ()) -> list[sqlite3.Row]:
    """Harvested alphas that already looked regular-grade in old sims (any shape)."""
    _, own = collide_stems(conn, account)
    o_ids, o_stems, o_exprs = other_accounts(conn, account, list(others))
    statuses = ("pp_ready", "pp_multi", "purify", "rewrite")
    rows = shortlist(conn, None, n=n * 3 if o_ids else n, statuses=statuses, account=account, skip_done=True,
                     exclude_stems=own | o_stems, exclude_exprs=submitted_exprs(conn, account) | o_exprs,
                     min_sharpe=REGULAR_SHARPE, min_fitness=REGULAR_FITNESS,
                     max_per_dataset=3, max_per_shape=2, max_sharpe=SUSPICIOUS_SHARPE)
    return _without(rows, o_ids, n)


def tune_variants(settings: dict, fails: list[str], k: int = 4) -> list[tuple[str, dict]]:
    """Settings-only variants aimed at the failing regular checks."""
    out = []

    def add(why, **ch):
        s = dict(settings, **ch)
        if s != settings and all(s != o for _, o in out):
            out.append((why, s))

    neut = settings.get("neutralization")
    if "CONCENTRATED_WEIGHT" in fails:
        add("truncation 0.05 against concentrated weight", truncation=0.05)
        add("truncation 0.03", truncation=0.03)
    if "LOW_FITNESS" in fails or "HIGH_TURNOVER" in fails:
        add("slower decay to cut turnover", decay=max(8, (settings.get("decay") or 0) * 2))
        add("decay 15", decay=15)
    if "LOW_SHARPE" in fails or "LOW_SUB_UNIVERSE_SHARPE" in fails:
        for nn in ("SUBINDUSTRY", "INDUSTRY", "SECTOR", "MARKET"):
            if nn != neut:
                add(f"neutralization {nn}", neutralization=nn)
        if settings.get("universe") == "TOP3000":
            add("universe TOP1000", universe="TOP1000")
    return out[:k]


def near_misses(conn: sqlite3.Connection, account: str) -> list[sqlite3.Row]:
    rows = conn.execute("""select r.* from resims r where r.account=? and r.status='done'
                           and r.id not in (select coalesce(parent_resim_id, -1) from resims)""", (account,)).fetchall()
    return [r for r in rows if regular_verdict(r["checks"])[0] == "near"]


def is_pp(row: sqlite3.Row) -> bool:
    return verdict(row["sharpe"], row["checks"])[0] == "pp_ok"
