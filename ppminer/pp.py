"""Power Pool eligibility checks that can be done locally, before simulating.

Platform rules (ref/Power Pool Alphas _ WorldQuant BRAIN.pdf):
  Sharpe >= 1.0, <= 8 operators (ts_backfill/group_backfill free),
  <= 3 unique data fields (grouping fields excluded), PP correlation < 0.5,
  turnover tests pass, theme match for pure PP alphas after week 6.
Sharpe/correlation/turnover need a simulation; everything else is checked here.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import Catalog
from .expr import GROUPING_FIELDS, analyze, lint

MAX_OPS = 8
MAX_FIELDS = 3
MIN_SHARPE = 1.0


@dataclass
class PPCheck:
    expr: str
    parsed: bool
    canonical: str = ""
    n_ops: int = 0
    ops: list[str] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    datasets: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    unknown_ops: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def shape_ok(self) -> bool:
        return self.parsed and not self.problems

    @property
    def single_category(self) -> bool:
        return len(self.categories) == 1

    @property
    def single_dataset(self) -> bool:
        return len(self.datasets) == 1


def submit_expr(expr: str, sharpe: float | None) -> str:
    """Expression we would actually submit: sign-flipped when the old sim had negative sharpe."""
    return f"-({expr})" if (sharpe or 0) < 0 else expr


def check(expr: str, catalog: Catalog, region: str | None = None) -> PPCheck:
    s = analyze(expr)
    if not s.ok:
        return PPCheck(expr, parsed=False, problems=[f"parse: {s.error}"])
    fields, unknown = [], []
    for name in sorted(s.names):
        if name in GROUPING_FIELDS:
            continue
        (fields if catalog.get(name, region) else unknown).append(name)
    infos = [catalog.get(f, region) for f in fields]
    r = PPCheck(
        expr, parsed=True, canonical=s.canonical, n_ops=s.n_ops, ops=s.ops, fields=fields,
        categories=sorted({i.category for i in infos}), datasets=sorted({i.dataset for i in infos}),
        unknown=unknown,
        unknown_ops=sorted({o for o in s.ops if catalog.operators and o not in catalog.operators}),
    )
    if r.n_ops > MAX_OPS:
        r.problems.append(f"ops {r.n_ops} > {MAX_OPS}")
    if len(fields) + len(unknown) > MAX_FIELDS:
        r.problems.append(f"fields {len(fields) + len(unknown)} > {MAX_FIELDS}")
    if unknown:
        r.problems.append("unknown fields: " + ",".join(unknown))
    if not fields and not unknown:
        r.problems.append("no data field")
    r.problems += lint(expr, lambda n: (i := catalog.get(n, region)) is not None and i.type == "VECTOR")
    return r
