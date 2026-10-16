"""Engine functions called directly: schema inference, aggregation, the transform pipeline,
number parsing and the expression grammar."""

from __future__ import annotations

import pandas as pd
import pytest


@pytest.fixture(scope="module")
def df():
    return pd.DataFrame(
        {
            "Region": ["North", "South", "North", "West", "South", "North"],
            "Product": ["A", "B", "A", "C", "B", "A"],
            "Order Date": ["2025-01-05", "2025-01-20", "2025-02-11", "2025-02-15", "2025-03-01", "2025-03-22"],
            "Units": [10, 5, 8, 3, 12, 7],
            "Revenue": ["$1,000.00", "$500.00", "$800.00", "$300.00", "$1,200.00", "$700.00"],
            "Note": ["ok", "ok", "late", "ok", "late", "ok"],
        }
    )


def test_classify_roles(engine, df):
    roles = engine._classify(df)
    assert roles["Revenue"]["role"] == "measure"  # currency string -> numeric measure
    assert roles["Units"]["role"] == "measure"
    assert roles["Region"]["role"] == "dimension"
    assert roles["Order Date"]["role"] == "temporal"


def test_locale_tolerant_numeric(engine):
    s = pd.Series(["$1,000.00", "(200)", "1.234,56", "12%"])
    out = engine._coerce_numeric(s)
    assert out.iloc[0] == 1000.0
    assert out.iloc[1] == -200.0  # parenthesis negative
    assert out.iloc[2] == 1234.56  # European style: '.' thousands, ',' decimal
    assert out.iloc[3] == 12.0  # percent keeps the shown value


def test_header_detection(engine):
    raw = pd.DataFrame(
        [
            ["Quarterly Report", None, None],  # title/banner row
            [None, None, None],  # blank row
            ["Region", "Units", "Revenue"],  # real header
            ["North", 10, 1000],
        ]
    )
    assert engine._detect_header(raw) == 2


def test_group_aggregate(engine, df):
    res = engine._do_group(df, ["Region"], {"revenue": "sum:Revenue", "orders": "count"})
    north = res[res["Region"] == "North"].iloc[0]
    assert north["revenue"] == 2500.0  # 1000 + 800 + 700
    assert north["orders"] == 3


def test_multi_agg(engine, df):
    out = engine._apply_aggs(engine._coerce_numeric(df["Units"]), ["sum", "mean", "count", "max"])
    assert out["sum"] == 45.0
    assert out["count"] == 6.0
    assert out["max"] == 12.0


def test_transform_pipeline(engine, df):
    work, derived, log = engine._run_pipeline(
        df,
        [
            {"op": "derive", "name": "PerUnit", "expr": "num(`Revenue`) / `Units`"},
            {"op": "filter", "expr": "`Units` >= 8"},
            {"op": "select", "columns": ["Region", "Units", "PerUnit"]},
            {"op": "sort", "by": "Units", "dir": "desc"},
            {"op": "limit", "n": 2},
        ],
        {},
    )
    assert "PerUnit" in derived
    assert list(work.columns) == ["Region", "Units", "PerUnit"]
    assert len(work) == 2
    assert work.iloc[0]["Units"] == 12  # sorted desc, limited to 2


def test_window_share(engine, df):
    work = engine._do_window(df, {"fn": "share", "value": "Units", "by": ["Region"], "name": "share"})
    # shares within a group must sum to ~100
    north = work[work["Region"] == "North"]["share"].sum()
    assert round(north, 4) == 100.0


def test_safe_expr_computes(engine, df):
    val = engine._SafeExpr(df).eval("`Units` * 2")
    assert list(val) == [20, 10, 16, 6, 24, 14]


@pytest.mark.parametrize(
    "expr",
    [
        "__import__('os').system('echo hi')",
        "(1).__class__",
        "open('x')",
        "[x for x in range(3)]",
        "lambda: 1",
    ],
)
def test_safe_expr_rejects_dangerous_syntax(engine, df, expr):
    with pytest.raises(engine._ExprError):
        engine._SafeExpr(df).eval(expr)
