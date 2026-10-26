"""Verify a Power Pool description against its expression before it goes on BRAIN.

Qwen's descriptions are usually good but on 24 Oct 2 of 4 got something wrong
(direction of a stability signal, "trading volume" for VWAP, a 60-day window
called six months, a guessed field meaning). Automatic submission therefore
requires:
  1. deterministic checks: window lengths and field names mentioned in the text
     must occur in the expression; required sections present; >= 100 chars
  2. a second Qwen pass (reasoning on) that compares the text with the
     expression and the catalog's field descriptions
A failed description gets one rewrite with the problems fed back.
"""
from __future__ import annotations

import json
import re

from .catalog import Catalog
from .describe import MIN_LEN, SCHEMA as DESC_SCHEMA, SYSTEM as DESC_SYSTEM, compose
from .expr import analyze
from .llm import chat_json

SECTIONS = ("Idea:", "Rationale for data used:", "Rationale for operators used:")
_WINDOW = re.compile(r"\b(\d{1,3})[- ](?:day|days|trading[- ]day|d)\b", re.I)
_FIELDLIKE = re.compile(r"`?\b([a-z][a-z0-9]*(?:_[a-z0-9]+){2,})\b`?")

VERIFY_SYSTEM = """You audit descriptions of WorldQuant BRAIN alphas before submission. Compare the description
with the expression and the field definitions. Flag ONLY factual errors that matter:
- wrong direction (what is long vs short, including the effect of leading minus signs / reverse)
- wrong window lengths or horizons (e.g. 60 days described as six months)
- fields described as something they are not (check against the field definitions)
- operators described incorrectly (e.g. ts_std_dev described as a level, ts_rank as cross-sectional)
- claims about performance or returns
Do not flag style, length or vague-but-not-wrong wording.
In "analysis", first work out step by step what the expression computes and which stocks it is
long, then compare each claim in the description with that. Then give the verdict."""

# "analysis" comes first so the model works through direction, windows, fields and
# operators before it commits to a verdict (hidden thinking + JSON mode returned empty replies)
VERIFY_SCHEMA = {"type": "object", "properties": {
    "analysis": {"type": "string"},
    "accurate": {"type": "boolean"},
    "issues": {"type": "array", "items": {"type": "string"}}}, "required": ["analysis", "accurate", "issues"]}


def rule_issues(expr: str, text: str) -> list[str]:
    issues = []
    if len(text) < MIN_LEN:
        issues.append(f"shorter than {MIN_LEN} characters")
    for s in SECTIONS:
        if s not in text:
            issues.append(f"missing section '{s}'")
    # digits anywhere count: horizons are often part of field names (implied_volatility_call_120)
    numbers = set(re.findall(r"\d+", expr))
    for w in _WINDOW.findall(text):
        if w not in numbers:
            issues.append(f"mentions a {w}-day window that is not in the expression")
    s = analyze(expr)
    names = s.names if s.ok else set()
    for f in set(_FIELDLIKE.findall(text)):
        if f not in names and f not in expr:
            issues.append(f"names field '{f}' which is not in the expression")
    return issues


# Semantics Qwen gets wrong without help (seen 26 Oct: "zeroed out" / "goes short" for trade_when's -1)
OPERATOR_NOTES = {
    "trade_when": "trade_when(x, y, z): if z > 0 the position is closed (NaN); else if x > 0 the value is y; "
                  "otherwise the PREVIOUS value is kept. z = -1 means never close, so the last position is held.",
    "reverse": "reverse(x) = -x; a leading minus sign is the same operator.",
}


def _operator_lines(expr: str, catalog: Catalog) -> str:
    """BRAIN's own definitions of the operators used, so text and audit agree with the platform."""
    s = analyze(expr)
    out = []
    for op in sorted(set(s.ops) if s.ok else []):
        doc = OPERATOR_NOTES.get(op) or getattr(catalog, "op_docs", {}).get(op)
        if doc:
            out.append(f"- {doc[:500]}")
    if expr.lstrip().startswith("-"):
        out.append("- The expression starts with a minus sign: the whole signal is inverted.")
    return "\n".join(out) or "(none)"


def _field_lines(expr: str, catalog: Catalog, region: str) -> str:
    s = analyze(expr)
    out = []
    for f in sorted(s.names if s.ok else []):
        i = catalog.get(f, region)
        if i:
            out.append(f"- {f} ({i.category}/{i.dataset}, {i.type}): {i.description}")
    return "\n".join(out) or "(no catalog entries)"


def verify(expr: str, text: str, catalog: Catalog, region: str, settings: dict | None = None) -> tuple[bool, list[str]]:
    issues = rule_issues(expr, text)
    if issues:
        return False, issues
    prompt = f"""Expression: {expr}
Settings: {json.dumps(settings or {})}
Field definitions:
{_field_lines(expr, catalog, region)}
Operator definitions (from BRAIN):
{_operator_lines(expr, catalog)}

Description:
{text}"""
    out = chat_json([{"role": "system", "content": VERIFY_SYSTEM}, {"role": "user", "content": prompt}],
                    VERIFY_SCHEMA, max_tokens=3000, temperature=0.0)
    return bool(out.get("accurate")) and not out.get("issues"), list(out.get("issues") or [])


def rewrite(expr: str, issues: list[str], catalog: Catalog, region: str, settings: dict | None = None) -> str:
    """One more description attempt, told what was wrong with the last one."""
    prompt = f"""Alpha expression (BRAIN FASTEXPR, region {region}): {expr}
(Positive values = long. Explain the direction of THIS expression, including any leading minus sign.)
Settings: {json.dumps(settings or {})}
Field definitions:
{_field_lines(expr, catalog, region)}
Operator definitions (from BRAIN):
{_operator_lines(expr, catalog)}

A previous description had these errors - avoid them:
{chr(10).join('- ' + i for i in issues)}

Only mention window lengths that appear in the expression. Return JSON with idea, rationale_data, rationale_ops."""
    d = chat_json([{"role": "system", "content": DESC_SYSTEM}, {"role": "user", "content": prompt}], DESC_SCHEMA)
    return compose(d)


def verified_description(expr: str, draft: str | None, catalog: Catalog, region: str,
                         settings: dict | None = None) -> tuple[str | None, list[str]]:
    """Return (text, []) when a description passes, else (None, issues)."""
    text = draft or rewrite(expr, [], catalog, region, settings)
    ok, issues = verify(expr, text, catalog, region, settings)
    if ok:
        return text, []
    all_issues = list(issues)
    for attempt in (1, 2):                     # two rewrites, each told every problem found so far
        text = rewrite(expr, all_issues, catalog, region, settings)
        ok, new = verify(expr, text, catalog, region, settings)
        if ok:
            return text, []
        all_issues += [f"rewrite {attempt}: {i}" for i in new]
    return None, all_issues
