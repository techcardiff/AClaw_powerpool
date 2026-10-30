from ppminer.expr import analyze, lint, skeleton


def ops(e):
    s = analyze(e)
    assert s.ok, s.error
    return s.n_ops


def test_function_and_infix_ops_counted():
    assert ops("rank(close)") == 1
    assert ops("ts_rank(close / volume, 20)") == 2
    assert ops("-ts_delta(close, 5)") == 2          # reverse + ts_delta
    assert ops("close * -1") == 1                    # -1 is a constant
    assert ops("a > 0 ? rank(a) : -rank(a)") == 5    # if_else, greater, rank, reverse, rank


def test_backfill_is_free():
    assert ops("rank(ts_backfill(anl4_eps, 60))") == 1
    assert ops("group_rank(group_backfill(x, industry, 20), subindustry)") == 1


def test_kwargs_and_names():
    s = analyze("ts_regression(y, x, 20, lag=0, rettype=2) + vec_avg(nws_score)")
    assert s.ok
    assert s.names == {"y", "x", "nws_score"}
    s = analyze("winsorize(rank(x), std=4)")
    assert s.names == {"x"}


def test_statements_and_variables():
    s = analyze("a = ts_mean(close, 5); b = rank(a); b - ts_delay(b, 1)")
    assert s.ok
    assert s.names == {"close"}
    assert s.n_ops == 4      # ts_mean, rank, subtract, ts_delay


def test_sci_notation_and_parse_errors():
    assert analyze("x + 1e-6").ok
    assert not analyze("rank(x").ok


def test_skeleton_ignores_windows():
    tag = lambda n: "F"
    assert skeleton("ts_rank(x, 20)", tag) == skeleton("ts_rank(y, 60)", tag)
    assert skeleton("ts_rank(x, 20)", tag) != skeleton("ts_zscore(x, 20)", tag)


def test_lint_vector_and_identical_pairs():
    vec = lambda n: n == "nws"
    assert lint("rank(vec_avg(nws))", vec) == []
    assert lint("rank(nws)", vec) == ["vector field nws not wrapped in vec_*"]
    assert lint("ts_corr(x, x, 20)", vec) == ["ts_corr of identical inputs"]
    assert lint("ts_corr(x, y, 20)", vec) == []


def test_lint_scientific_notation():
    assert lint("x / (y + 1e-8)", lambda n: False) == ["scientific notation 1e-8 (BRAIN rejects it; write 0.00000001)"]


def test_description_rules():
    from ppminer.descheck import rule_issues
    expr = "ts_rank(divide(anl4_x_mean, vwap), 252)"
    good = "Idea: x. Rationale for data used: anl4_x_mean over vwap. Rationale for operators used: a 252-day rank." + " " * 20
    assert rule_issues(expr, good) == []
    bad = good.replace("252-day", "60-day").replace("anl4_x_mean", "anl4_other_field")
    issues = rule_issues(expr, bad)
    assert any("60-day" in i for i in issues) and any("anl4_other_field" in i for i in issues)


def test_atom_verdict():
    import json
    from ppminer.verdict import atom_verdict
    chk = lambda **kw: json.dumps([{"name": k, "result": v} for k, v in kw.items()])
    ok = chk(LOW_SHARPE="PASS", IS_LADDER_SHARPE="FAIL", SELF_CORRELATION="PASS", REGULAR_SUBMISSION="FAIL")
    assert atom_verdict(ok, True) == ("atom_ok", [])
    assert atom_verdict(ok, False)[0] == "not_atom"
    assert atom_verdict(chk(LOW_FITNESS="FAIL"), True) == ("not_atom", ["LOW_FITNESS"])
    assert atom_verdict(chk(SELF_CORRELATION="PENDING"), True)[0] == "pending"


def test_description_window_inside_field_name():
    from ppminer.descheck import rule_issues
    expr = "ts_mean(implied_volatility_call_120 / implied_volatility_put_120, 60)"
    text = ("Idea: 120-day implied volatility skew. Rationale for data used: implied_volatility_call_120 and "
            "implied_volatility_put_120. Rationale for operators used: a 60-day mean.")
    assert rule_issues(expr, text) == []


def test_quality_gate_on_real_alphas():
    import json
    from ppminer.verdict import quality_gate
    chk = lambda sub, y2, conc="PASS": json.dumps([{"name": "LOW_SUB_UNIVERSE_SHARPE", "value": sub},
                                                   {"name": "LOW_2Y_SHARPE", "value": y2},
                                                   {"name": "CONCENTRATED_WEIGHT", "result": conc}])
    assert quality_gate(2.16, 2.11, 0.05, chk(1.01, None)) == []          # pwvLrzWv: strong
    assert quality_gate(2.24, 2.27, 0.05, chk(1.00, 3.52)) == []          # JjPrA2k2: strong
    weak = quality_gate(1.03, 0.23, 0.57, chk(0.57, 1.48))                # XgRe5ex5: weak
    assert any("sharpe" in w for w in weak) and any("fitness" in w for w in weak) and any("turnover" in w for w in weak)
    assert quality_gate(1.42, 1.09, 0.02, chk(0.64, 2.09)) != []          # xA15d0dW: middle, sharpe and sub-universe
    assert quality_gate(1.42, 1.09, 0.02, chk(0.64, 2.09), {"min_sharpe": 1.3, "min_sub_universe_sharpe": 0.6}) == []


def test_tune_variants_skip_tried_and_prefer_liquid_universe():
    from ppminer.lanes import tune_variants
    base = {"universe": "TOP3000", "neutralization": "SUBINDUSTRY", "decay": 0, "truncation": 0.08}
    v = tune_variants(base, ["LOW_SUB_UNIVERSE_SHARPE"], k=3)
    assert v[0][1]["universe"] == "TOP1000"
    tried = [s for _, s in v]
    again = tune_variants(base, ["LOW_SUB_UNIVERSE_SHARPE"], k=3, tried=tried)
    assert all(s not in tried for _, s in again)


def test_funnel_fixes_target_the_failing_check():
    from ppminer.funnel import fixes
    s = {"universe": "TOP3000", "neutralization": "SUBINDUSTRY", "decay": 0, "truncation": 0.08}
    out = fixes("rank(x)", s, ["LOW_SUB_UNIVERSE_SHARPE"], {})
    whys = [w for w, _, _ in out]
    assert whys[0] == "liquidity: trade liquid names only" and not any("size buckets" in w for w in whys)
    assert any(st.get("universe") == "TOP1000" for _, _, st in out)
    assert fixes("rank(x)", s, ["SELF_CORRELATION", "LOW_SUB_UNIVERSE_SHARPE"], {}) == []
    faded = {"LOW_2Y_SHARPE": {"result": "FAIL", "value": 0.3}}
    assert fixes("rank(x)", s, ["LOW_SUB_UNIVERSE_SHARPE"], faded) == []
    conc = fixes("x", s, ["CONCENTRATED_WEIGHT"], {})
    assert conc[0][1] == "rank(x)" and any(st.get("truncation") == 0.05 for _, _, st in conc)


def test_templates_generate_single_dataset_expressions():
    import sqlite3
    from ppminer.catalog import Catalog, FieldInfo
    from ppminer.templates import generate
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    conn.execute("""create table account_fields (account, region, delay, universe, field, dataset, category, type,
                    description, coverage, user_count, alpha_count, synced_at)""")
    for f, cov in (("eps_a", 0.95), ("eps_b", 0.5), ("eps_c", 0.99)):
        conn.execute("insert into account_fields values ('A','EUR',1,'TOP2500',?,'ds1','analyst','MATRIX','',?,1,1,null)", (f, cov))
    cat = Catalog("/nonexistent")
    for f in ("eps_a", "eps_b", "eps_c"):
        cat.by_region.setdefault("EUR", {})[f] = FieldInfo(f, "analyst", "ds1", "MATRIX", "EUR")
    s1 = generate(conn, cat, "A", "EUR", "s1", "ds1", n_fields=3)
    assert s1 and all(not r["problems"] for r in s1)
    assert any("ts_backfill(eps_b, 60)" in r["expr"] for r in s1)          # sparse field backfilled
    assert any(r["expr"].endswith(", country)") for r in s1)              # multi-country region
    s2 = generate(conn, cat, "A", "EUR", "s2", "ds1", n_fields=3)
    assert any(r["expr"].startswith("ts_corr(") for r in s2) and all(r["datasets"] == ["ds1"] for r in s2)


def test_template_field_order_and_picked_fields():
    import sqlite3
    from ppminer.catalog import Catalog, FieldInfo
    from ppminer.templates import generate, top_fields
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    conn.execute("""create table account_fields (account, region, delay, universe, field, dataset, category, type,
                    description, coverage, user_count, alpha_count, synced_at)""")
    rows = [("pop", "d1", 0.9, 300), ("rare", "d1", 0.8, 2), ("sparse", "d1", 0.3, 999), ("other", "d2", 0.95, 50)]
    cat = Catalog("/nonexistent")
    for f, ds, cov, alphas in rows:
        conn.execute("insert into account_fields values ('A','EUR',1,'TOP2500',?,?,'analyst','MATRIX','',?,1,?,null)",
                     (f, ds, cov, alphas))
        cat.by_region.setdefault("EUR", {})[f] = FieldInfo(f, "analyst", ds, "MATRIX", "EUR")
    assert [r["field"] for r in top_fields(conn, "A", "EUR", "d1", 3, "alphas")] == ["pop", "rare"]   # sparse dropped
    assert [r["field"] for r in top_fields(conn, "A", "EUR", "d1", 3, "fresh")] == ["rare", "pop"]
    s2 = generate(conn, cat, "A", "EUR", "s2", fields=["pop", "rare", "other"])
    assert s2 and all("other" not in r["expr"] for r in s2)          # no cross-dataset pairs
    s1 = generate(conn, cat, "A", "EUR", "s1", fields=["pop", "other"])
    assert {d for r in s1 for d in r["datasets"]} == {"d1", "d2"} and all(not r["problems"] for r in s1)


def test_quality_gate_blocks_degenerate_alpha():
    import json
    from ppminer.verdict import quality_gate
    # pwv2QXVj, 26 Oct: sharpe 3.88, fitness 14.9, returns 301%, half the weight in one stock
    chk = json.dumps([{"name": "CONCENTRATED_WEIGHT", "result": "WARNING", "value": 0.5},
                      {"name": "LOW_SUB_UNIVERSE_SHARPE", "value": 3.77}])
    low = quality_gate(3.88, 14.91, 0.2042, chk, returns=3.0149)
    assert any("concentrated" in x for x in low) and any("fitness" in x for x in low) and any("returns" in x for x in low)


def test_pp_fixes_smooth_high_turnover_within_power_pool_shape():
    from ppminer.funnel import pp_fixes
    s = {"universe": "TOP3000", "neutralization": "SUBINDUSTRY", "decay": 0, "truncation": 0.08}
    out = pp_fixes("group_rank(ts_rank(x, 60), country)", s, 0.36)
    assert out[0][1] == "ts_decay_linear(group_rank(ts_rank(x, 60), country), 5)"
    assert any(st.get("decay") == 10 for _, _, st in out)
    calm = pp_fixes("rank(x)", s, 0.02)                     # low turnover: smoothing can't help fitness
    assert calm and all("neutralization" in w for w, _, _ in calm)
    long = "rank(ts_rank(ts_zscore(ts_mean(ts_delta(ts_std_dev(x, 5), 5), 5), 5), 5))" + " + rank(x)" * 1
    assert all(e != f"ts_decay_linear({long}, 5)" for _, e, _ in pp_fixes(long, s, 0.3))   # would be 9 ops
