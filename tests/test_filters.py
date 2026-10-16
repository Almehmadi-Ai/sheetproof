"""The `filters` parameter is model-supplied text. It must never reach DataFrame.query /
pandas.eval / eval: it is evaluated by the same AST allow-list grammar as transform
expressions. These tests pin both halves of that contract — every known exploit shape is
rejected with a clear error, and legitimate filters return exactly what pandas returns."""

from __future__ import annotations

import time

import pandas as pd
import pytest
from conftest import ALICE, call, revenue_numbers


def _filtered_count(tool, sales_id, filters, params=None):
    res = call(
        tool,
        "analyze_spreadsheet",
        ALICE,
        operation="aggregate",
        agg="count",
        filters=filters,
        params=params,
        file_id=sales_id,
    )
    assert res.get("status") == "ok", res
    assert res["result"]["count"] == res["provenance"]["rows_after_filter"]
    return res["result"]["count"]


# --------------------------------------------------------------------------- #
# Exploits: every payload is rejected before anything executes
# --------------------------------------------------------------------------- #
EXPLOITS = [
    "@os.system('echo pwned > {canary}') == 0 or Units > 0",  # DataFrame.query resolves @ to module globals
    "@pd.read_csv('{secret}').shape[0] > 0",  # ... including pandas I/O
    "@json.dumps(1) == '1'",
    "@glob.glob('*') == []",
    "@__builtins__ is None",
    "@open('{secret}')",
    "__import__('os').system('echo pwned > {canary}') == 0",
    "Units.__class__.__mro__[1].__subclasses__() == []",
    "Units.__class__ == 1",
    "Units.str.__class__ == 1",
    "Units.values == 1",
    "Units.to_csv('{canary}') == 0",  # pandas method with a side effect
    "Units.apply(print) == 0",
    "Units.pipe(print) == 0",
    "Units.isna().sum() > 0",  # chaining past the allow-listed method
    "getattr(Units, 'to_csv')('{canary}')",
    "eval('1')",
    "exec('import os')",
    "open('{secret}').read() == ''",
    "(lambda: 1)() == 1",
    "[x for x in Units] == []",
    "{{x for x in Units}} == 1",
    "sum(x for x in [1]) == 1",
    "Units[0] == 1",
    "(a := 1) == 1",
    "Units if True else Units",
    "f'{{Units}}' == ''",
    "b'x' == Units",
    "Units.isna(**{{'a': 1}})",
    "col('Units', x=1) > 0",
    "Units.str.contains('a', regex=True)",
    "Units > 9**9**9",  # huge-int DoS
    "Units > 10**10**10",
    "Units > 2**100000",
    "Units > " + "9" * 1900,  # huge integer literal
    "'a' * 999999999999 == Units",  # string repetition DoS
    "[1] * 999999999 == Units",  # list repetition DoS
    "not " * 400 + "True",  # deep nesting
    "(" * 300 + "Units" + ")" * 300 + " > 1",
    "Units > 1 and " * 200 + "True",  # too long / too complex
]


@pytest.mark.parametrize("payload", EXPLOITS)
def test_exploit_filters_are_rejected(tool, sales_id, tmp_path, payload):
    canary = tmp_path / "canary.txt"
    secret = tmp_path / "server_secret.csv"
    secret.write_text("token\nTOP-SECRET-VALUE\n", encoding="utf-8")
    filters = payload.format(canary=canary.as_posix(), secret=secret.as_posix())
    started = time.perf_counter()
    res = call(
        tool,
        "analyze_spreadsheet",
        ALICE,
        operation="aggregate",
        value_column="Units",
        filters=filters,
        file_id=sales_id,
    )
    elapsed = time.perf_counter() - started
    assert res["status"] == "error", res
    assert res["reason"].startswith("filter error"), res
    assert "TOP-SECRET-VALUE" not in str(res)
    assert not canary.exists()
    assert elapsed < 2.0, f"rejection took {elapsed:.2f}s"


def test_filters_never_use_query_or_eval(tool, sales_id, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("model input must never reach DataFrame.query / DataFrame.eval / pandas.eval")

    monkeypatch.setattr(pd.DataFrame, "query", forbidden)
    monkeypatch.setattr(pd.DataFrame, "eval", forbidden)
    monkeypatch.setattr(pd, "eval", forbidden)
    assert _filtered_count(tool, sales_id, "Region == 'North' and Units > 8") == 3


def test_exploits_are_also_rejected_in_transform_steps(tool, sales_id, tmp_path):
    canary = tmp_path / "canary.txt"
    for step in (
        {"op": "filter", "expr": f"Units.to_csv('{canary.as_posix()}') == 0"},
        {"op": "derive", "name": "x", "expr": "9**9**9"},
        {"op": "derive", "name": "x", "expr": "@os"},
    ):
        res = call(tool, "analyze_spreadsheet", ALICE, operation="transform", pipeline=[step], file_id=sales_id)
        assert res["status"] == "error", res
        assert res["reason"].startswith("pipeline error"), res
    assert not canary.exists()


def test_unknown_parameter_is_reported_not_resolved(tool, sales_id):
    res = call(
        tool,
        "analyze_spreadsheet",
        ALICE,
        operation="aggregate",
        value_column="Units",
        filters="Units > @os",
        file_id=sales_id,
    )
    assert res["status"] == "error"
    assert "unknown parameter @os" in res["reason"]


# --------------------------------------------------------------------------- #
# Legitimate filters match independent pandas results
# --------------------------------------------------------------------------- #
LEGIT = [
    ("Region == 'North' and Units > 8", lambda d: (d.Region == "North") & (d.Units > 8)),
    ("`Order Date` >= '2025-03-01'", lambda d: pd.to_datetime(d["Order Date"]) >= "2025-03-01"),
    (
        "`Order Date`.between('2025-02-01', '2025-04-30')",
        lambda d: pd.to_datetime(d["Order Date"]).between("2025-02-01", "2025-04-30"),
    ),
    ("Region in ['North', 'West']", lambda d: d.Region.isin(["North", "West"])),
    ("Region not in ['North', 'West']", lambda d: ~d.Region.isin(["North", "West"])),
    ("Region.isna()", lambda d: d.Region.isna()),
    ("Region.notna() and Units >= 10", lambda d: d.Region.notna() & (d.Units >= 10)),
    ("Region != 'North'", lambda d: d.Region != "North"),  # blanks are != (pandas semantics)
    ("not (Region == 'North')", lambda d: ~(d.Region == "North")),
    ("(Region == 'North') | (Region == 'South')", lambda d: (d.Region == "North") | (d.Region == "South")),
    ("(Units > 5) & ~(Region == 'South')", lambda d: (d.Units > 5) & ~(d.Region == "South")),
    ("Product.str.contains('Desk')", lambda d: d.Product.str.contains("Desk", regex=False)),
    ("Product.str.contains('desk', case=False)", lambda d: d.Product.str.lower().str.contains("desk")),
    ("Product.str.startswith(('Mo', 'Ke'))", lambda d: d.Product.str.startswith(("Mo", "Ke"))),
    ("Product.str.lower().str.endswith('lamp')", lambda d: d.Product.str.lower().str.endswith("lamp")),
    ("Units.between(5, 12)", lambda d: d.Units.between(5, 12)),
    ("Units.isin([1, 2, 3])", lambda d: d.Units.isin([1, 2, 3])),
    ("2 < Units <= 10", lambda d: (d.Units > 2) & (d.Units <= 10)),
    ("Units * 2 > 20", lambda d: d.Units * 2 > 20),
    ("Revenue > 900", lambda d: revenue_numbers(d) > 900),  # currency text compared as numbers
    ("Revenue >= 1000 and Revenue < 3000", lambda d: revenue_numbers(d).between(1000, 3000, inclusive="left")),
    ("Email == 'g@x.com'", lambda d: d.Email == "g@x.com"),  # '@' inside a string is not a parameter
    ("`Order ID` == 'ORD-7'", lambda d: d["Order ID"] == "ORD-7"),
]


@pytest.mark.parametrize(("filters", "expected"), LEGIT, ids=[f for f, _ in LEGIT])
def test_legitimate_filters_match_pandas(tool, sales_id, sales_df, filters, expected):
    want = int(expected(sales_df).fillna(False).sum())
    assert _filtered_count(tool, sales_id, filters) == want


def test_filter_feeds_the_operation(tool, sales_id, sales_df):
    res = call(
        tool,
        "analyze_spreadsheet",
        ALICE,
        operation="aggregate",
        value_column="Revenue",
        agg="sum",
        filters="Region == 'North'",
        file_id=sales_id,
    )
    assert res["result"]["sum"] == round(revenue_numbers(sales_df)[sales_df.Region == "North"].sum(), 4)
    assert res["provenance"]["filters_applied"] == "Region == 'North'"


def test_operations_report_the_filtered_rows(tool, sales_id, sales_df):
    north = int((sales_df.Region == "North").sum())
    for operation in ("overview", "describe", "missing"):
        res = call(
            tool, "analyze_spreadsheet", ALICE, operation=operation, filters="Region == 'North'", file_id=sales_id
        )
        assert res["provenance"]["rows_processed"] == north, operation
        assert res["provenance"]["rows_total"] == len(sales_df)
    overview = call(
        tool, "analyze_spreadsheet", ALICE, operation="overview", filters="Region == 'North'", file_id=sales_id
    )
    assert overview["result"]["rows"] == north


def test_filter_parameters(tool, sales_id, sales_df):
    got = _filtered_count(
        tool,
        sales_id,
        "Units >= @min_units and Region in @regions",
        params={"min_units": 8, "regions": ["North", "South"]},
    )
    assert got == int(((sales_df.Units >= 8) & sales_df.Region.isin(["North", "South"])).sum())


def test_unicode_column_names_in_backticks(tool, owui):
    df = pd.DataFrame({"المنطقة": ["شمال", "جنوب", "شمال"], "الإيرادات": [100, 200, 300]})
    fid = owui.upload("alice", "ar.csv", df=df)
    res = call(
        tool,
        "analyze_spreadsheet",
        ALICE,
        operation="aggregate",
        value_column="الإيرادات",
        agg="sum",
        filters="`المنطقة` == 'شمال'",
        file_id=fid,
    )
    assert res["status"] == "ok", res
    assert res["result"]["sum"] == 400


@pytest.mark.parametrize(
    ("filters", "message"),
    [
        ("Units", "must be a condition"),
        ("Nope > 1", "unknown column 'Nope'"),
        ("Region in 'North'", "needs a list"),
        ("Units > 'many'", "cannot order the numeric column"),
        ("Units.between(1)", "takes 2-3 argument(s)"),
        ("Product.str.contains(Units)", "needs text"),
        ("`Region", "unterminated"),
        ("Units >", "cannot parse"),
    ],
)
def test_invalid_filters_explain_the_problem(tool, sales_id, filters, message):
    res = call(
        tool,
        "analyze_spreadsheet",
        ALICE,
        operation="aggregate",
        value_column="Units",
        filters=filters,
        file_id=sales_id,
    )
    assert res["status"] == "error"
    assert message in res["reason"], res
    assert "Region" in res["columns_available"]


# --------------------------------------------------------------------------- #
# Expression evaluator hardening shared with transform
# --------------------------------------------------------------------------- #
def test_pow_needs_a_bounded_literal_exponent(engine, sales_df):
    ev = engine._SafeExpr(sales_df)
    assert list(ev.eval("Units ** 2")[:3]) == [100.0, 25.0, 64.0]
    assert ev.eval("(9 ** 9) ** 9") == pytest.approx(387420489.0**9)
    assert pd.isna(ev.eval("(-8) ** 0.5"))  # float semantics, never a complex number
    for expr in ("9 ** 9 ** 9", "Units ** Units", "2 ** 101", "2 ** -101"):
        with pytest.raises(engine._ExprError):
            ev.eval(expr)


def test_text_is_never_repeated_and_concatenation_is_bounded(engine, sales_df):
    ev = engine._SafeExpr(sales_df, {"long": "x" * engine._MAX_TEXT_LEN})
    assert ev.eval("text(Region) + '-' + text(Product)").iloc[0] == "North-Desk"
    assert ev.eval("Product * 1000").isna().all()  # text columns are read as numbers, never repeated
    assert ev.eval("Revenue * 2").iloc[0] == 2000.0  # ... which is what makes currency text usable
    for expr in ("'ab' * 3", "[1] * 3", "text(Product) + @long", "'a' + 1"):
        with pytest.raises(engine._ExprError):
            ev.eval(expr)
