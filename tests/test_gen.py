"""Shape ideas on untried fields while Qwen is down (gen.py)."""
import json
from types import SimpleNamespace as NS

from ppminer import gen
from ppminer.db import connect


class Cat:
    """Catalog stand-in: every field in FIELDS is known, groups are not fields."""
    FIELDS = {}

    def get(self, f, region):
        return self.FIELDS.get(f)


def _db(tmp_path, fields):
    from ppminer import fields as fl
    conn = connect(str(tmp_path / "pp.db"))
    conn.executescript(fl.SCHEMA)
    cols = {r[1] for r in conn.execute("pragma table_info(account_fields)")}
    for f, ds, typ, cov in fields:
        conn.execute("insert or ignore into account_datasets(account, region, delay, dataset) values ('A','GLB',1,?)", (ds,))
        row = {"account": "A", "region": "GLB", "delay": 1, "field": f, "dataset": ds, "type": typ, "coverage": cov}
        row = {k: v for k, v in row.items() if k in cols}
        conn.execute(f"insert into account_fields({','.join(row)}) values ({','.join('?' * len(row))})", tuple(row.values()))
    conn.commit()
    return conn


def _patch(monkeypatch, known):
    from ppminer import pp
    Cat.FIELDS = {f: NS(category="Analyst", dataset=ds, type=t, description="") for f, ds, t, _ in known}
    monkeypatch.setattr(gen.lanes, "collide_stems", lambda c, a: (set(), set()))
    monkeypatch.setattr(gen.lanes, "other_accounts", lambda c, a, o: (set(), set(), set()))
    monkeypatch.setattr(gen.hints, "overused", lambda c: [])


def test_seeds_spread_over_datasets_and_skip_identifiers(monkeypatch, tmp_path):
    fields = [("anl_eps_up", "ds1", "MATRIX", 0.95), ("anl_eps_down", "ds1", "MATRIX", 0.9),
              ("rev_cons_value", "ds2", "VECTOR", 0.6), ("benchmark_currency_code_2", "ds2", "MATRIX", 0.99)]
    conn = _db(tmp_path, fields)
    _patch(monkeypatch, fields)
    monkeypatch.setattr(gen, "check", lambda e, c, r: NS(shape_ok=True, problems=[], unknown_ops=[], canonical=e,
                        n_ops=3, fields=[f for f, *_ in fields if f in e], categories=["analyst"],
                        datasets=sorted({d for f, d, *_ in fields if f in e}), single_category=True, single_dataset=True))
    assert gen.seeds(conn, Cat(), "A", "GLB", n=3) == 3
    rows = conn.execute("select expr, sources from candidates order by id").fetchall()
    exprs = [r[0] for r in rows]
    assert not any("currency" in e for e in exprs)
    # round robin: ds1, ds2, then ds1 again; shapes rotate; sparse VECTOR wrapped
    assert exprs[0] == "rank(ts_mean(anl_eps_up, 60))"
    assert exprs[1] == "rank(ts_delta(ts_backfill(vec_avg(rev_cons_value), 60), 20))"
    assert exprs[2].startswith("group_rank(ts_zscore(anl_eps_down")
    assert all(json.loads(r[1])[0].endswith(":seed") for r in rows)
    assert gen.seeds(conn, Cat(), "A", "GLB", n=3) == 0          # fields now used


def test_negative_seed_expands_flipped_and_only_once(monkeypatch, tmp_path):
    fields = [("anl_eps_up", "ds1", "MATRIX", 0.95)]
    conn = _db(tmp_path, fields)
    _patch(monkeypatch, fields)
    monkeypatch.setattr(gen, "check", lambda e, c, r: NS(shape_ok=True, problems=[], unknown_ops=[], canonical=e, n_ops=3,
                        fields=["anl_eps_up"], categories=["analyst"], datasets=["ds1"], single_category=True, single_dataset=True))
    gen.seeds(conn, Cat(), "A", "GLB", n=1)
    cid = conn.execute("select id from candidates").fetchone()[0]
    conn.execute("insert into resims(candidate_id, account, status, sharpe) values (?, 'A', 'done', -0.8)", (cid,))
    assert gen.expand(conn, Cat(), "A", "GLB") == len(gen.SHAPES) - 1
    exprs = {r[0] for r in conn.execute("select expr from candidates where sources like '%expand%'")}
    assert "-rank(ts_delta(anl_eps_up, 20))" in exprs and "rank(ts_std_dev(anl_eps_up, 60))" in exprs
    assert gen.expand(conn, Cat(), "A", "GLB") == 0


def test_identifier_names():
    for f in ("premap_instrument_to_ticker_map_twn", "report_date", "fx_code3"):
        assert gen.NOT_A_QUANTITY.search(f)
    for f in ("revenue_y2_consensus_value", "min_loan_rate_p5_d1", "holder_account_total"):
        assert not gen.NOT_A_QUANTITY.search(f)


def test_alongside_qwen_seeds_a_few_regardless_of_queue(monkeypatch):
    calls = []
    monkeypatch.setattr(gen, "expand", lambda c, cat, a, r, limit=10: calls.append(("expand", r, limit)) or 0)
    monkeypatch.setattr(gen, "seeds", lambda c, cat, a, r, o, t, n=30: calls.append(("seeds", r, n)) or n)
    monkeypatch.setattr(gen.lanes, "usable_ideas", lambda *a, **k: {"GLB": 999, "USA": 999})
    theme = NS(regions={"GLB"})
    out = gen.alongside(None, None, "A", ["USA", "GLB"], [], theme)
    assert out == {"GLB": gen.THEME_SEEDS_ALONGSIDE, "USA": gen.SEEDS_ALONGSIDE}
    assert calls[0] == ("expand", "GLB", gen.EXPAND_ALONGSIDE)
    assert gen.top_up(None, None, "A", ["USA", "GLB"], [], theme) == {}      # Qwen down: queue is full enough
