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
