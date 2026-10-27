import json
import sqlite3

from ppminer import hints, kg


def _db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""create table candidates (id integer primary key, region text, fields text, sharpe real, fitness real);
                       create table resims (id integer primary key, candidate_id int, status text, sharpe real, fitness real);
                       create table account_fields (account text, region text, delay int, field text, dataset text, alpha_count int);""")
    rows = [("USA", ["gem_a", "hub_b"], 2.0, 1.2)] * 4 + [("USA", ["gem_a"], 0.3, 0.1)] * 2 \
        + [("USA", ["hub_b", "weak_c"], 0.4, 0.2)] * 5 + [("USA", ["hub_b"], 1.6, 1.0)] * 3
    for region, fields, sh, fi in rows:
        c.execute("insert into candidates(region, fields, sharpe, fitness) values (?,?,?,?)", (region, json.dumps(fields), sh, fi))
    for f, ds, n in (("gem_a", "d1", 3), ("hub_b", "d2", 9000), ("weak_c", "d1", 1)):
        for region in ("USA", "GLB"):
            c.execute("insert into account_fields values ('A', ?, 1, ?, ?, ?)", (region, f, ds, n))
    kg._CACHE.update(at=0.0, graph=None)
    return c


def test_gems_are_proven_and_little_used():
    c = _db()
    tab = {r["field"]: r for r in kg.field_table(c, "A", "USA")}
    assert set(tab) == {"gem_a", "hub_b"}                    # weak_c never proven
    assert tab["gem_a"]["proven"] == 4 and tab["gem_a"]["n"] == 6
    assert [r["field"] for r in kg.gems(c, "A", "USA")] == ["gem_a"]
    assert kg.gems(c, "A", "USA", exclude={"gem"}) == []      # family already submitted


def test_other_regions_borrow_usa_evidence():
    c = _db()
    g = kg.gems(c, "A", "GLB")
    assert [r["field"] for r in g] == ["gem_a"] and g[0]["evidence"] == "USA"
    assert "proven in USA, untested here" in kg.block(c, "A", "GLB", "d1")


def test_placeholders_keep_structure_not_fields():
    expr = "group_rank(ts_mean(anl4_ptp_high / enterprise_value, 60), industry)"
    assert hints._placeholders(expr, ["enterprise_value", "anl4_ptp_high"]) == "group_rank(ts_mean(y / x, 60), industry)"


def test_overused_fields_are_hidden_from_qwen():
    from ppminer.explore import _fields
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""create table account_submitted (stems text, expr text);
                       create table account_fields (account text, region text, delay int, field text, dataset text, type text,
                                                    description text, coverage real, user_count int, alpha_count int);""")
    subs = [("vwap,cap,anl_x", "rank(vwap / cap) + anl_x_low"), ("vwap,cap,anl_x", "vwap * anl_x_high + cap"),
            ("vwap,cap,anl_x,fnd6", "group_rank(vwap - fnd6_tfvce + anl_x_mean, industry) / cap")]
    for stems, expr in subs:
        c.execute("insert into account_submitted values (?, ?)", (stems, expr))
    for f in ("vwap", "anl_x_mean", "anl_x_new", "fnd6_tfvce", "fnd6_newq_gem", "fresh_y"):
        c.execute("insert into account_fields values ('A', 'USA', 1, ?, 'd1', 'MATRIX', '', 1.0, 0, 0)", (f,))
    # vwap 3 uses; family anl_x* 3 uses; fnd6 only once and too generic as a family; cap kept for size buckets
    assert dict(hints.overused(c)) == {"vwap": 3, "anl_x*": 3}
    assert [r["field"] for r in _fields(c, "A", "USA", 1, "d1")] == ["fnd6_tfvce", "fnd6_newq_gem", "fresh_y"]
    assert "anl_x* (3)" in hints.overused_note(c)
