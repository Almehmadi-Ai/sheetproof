"""Column names supplied by the model are matched Unicode-aware, and a fuzzy match is only
used when it is clearly the intended column. Anything else is a needs_clarification that
lists the real columns — never a silent substitution."""

from __future__ import annotations

import pandas as pd
import pytest
from conftest import ALICE, call

ENGLISH = ["Order Date", "Region", "Net Revenue", "Gross Revenue", "Unit Price", "Units", "Cost"]
ARABIC = ["المنطقة", "الإيرادات", "التكلفة", "المبيعات", "مستوى الخدمة", "صافي الربح"]


@pytest.fixture
def resolve(plugin):
    gap = plugin.Tools.Valves().ambiguity_min_gap

    def _resolve(columns, name):
        return plugin._resolve_col(pd.DataFrame(columns=columns), name, gap)[0]

    return _resolve


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Units", "Units"),
        ("units", "Units"),
        ("order_date", "Order Date"),
        ("ORDER-DATE", "Order Date"),
        ("Date Order", "Order Date"),  # word order
        ("Unit Pirce", "Unit Price"),  # typo
        ("Regoin", "Region"),
        ("Cost", "Cost"),
        ("Date", "Order Date"),  # the only column containing the word
    ],
)
def test_english_names_resolve(resolve, name, expected):
    assert resolve(ENGLISH, name) == expected


@pytest.mark.parametrize(
    "name",
    [
        "Profit",  # not in the file
        "Revenue",  # Net Revenue vs Gross Revenue: too close to call
        "Price Unit Cost",
        "",
        "   ",
        "!!!",
    ],
)
def test_english_names_that_must_not_be_guessed(resolve, name):
    assert resolve(ENGLISH, name) is None


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("الإيرادات", "الإيرادات"),
        ("الايرادات", "الإيرادات"),  # alef with / without hamza
        ("الإيرادَات", "الإيرادات"),  # with a diacritic (fatha)
        ("الإيـــرادات", "الإيرادات"),  # tatweel
        ("التكلفه", "التكلفة"),  # taa marbuta written as haa
        ("مستوي الخدمة", "مستوى الخدمة"),  # alef maqsura vs yaa
        ("الايرادت", "الإيرادات"),  # misspelled: one letter missing
        ("صافي  الربح", "صافي الربح"),
        ("الربح", "صافي الربح"),  # the only column containing the word
    ],
)
def test_arabic_names_resolve(resolve, name, expected):
    assert resolve(ARABIC, name) == expected


@pytest.mark.parametrize("name", ["الكمية", "العميل", "التاريخ", "سعر"])
def test_arabic_names_that_must_not_be_guessed(resolve, name):
    assert resolve(ARABIC, name) is None


def test_missing_arabic_column_is_not_resolved_to_another(tool, owui):
    """An ASCII-only normalization maps every Arabic header to '', so profit (الربح) would match
    revenue (الإيرادات)."""
    df = pd.DataFrame({"المنطقة": ["شمال", "جنوب"], "الإيرادات": [100, 200], "التكلفة": [10, 20]})
    fid = owui.upload("alice", "ar.csv", df=df)
    res = call(tool, "analyze_spreadsheet", ALICE, operation="aggregate", value_column="الربح", file_id=fid)
    assert res["status"] == "needs_clarification"
    assert res["columns_available"] == ["المنطقة", "الإيرادات", "التكلفة"]
    ok = call(tool, "analyze_spreadsheet", ALICE, operation="aggregate", value_column="الايرادات", file_id=fid)
    assert ok["result"] == {"sum": 300.0, "value_column": "الإيرادات"}


def test_names_differing_only_in_case_are_ambiguous(plugin, resolve):
    col, cand = plugin._resolve_col(pd.DataFrame(columns=["Sales", "sales", "Cost"]), "SALES", 0.15)
    assert col is None and set(cand) == {"Sales", "sales"}
    assert resolve(["Sales", "sales"], "sales") == "sales"  # exact wins


@pytest.mark.parametrize(
    ("columns", "name"),
    [
        (["Revenue 2024", "Cost"], "Revenue 2026"),  # a different year is a different column
        (["Q1 Sales", "Region"], "Q2 Sales"),
        (["Revenue", "Cost"], "Revenue 2024"),  # a year-specific ask is not the generic column
    ],
)
def test_numbers_in_names_must_match(resolve, columns, name):
    assert resolve(columns, name) is None


def test_ambiguity_gap_valve(tool, owui):
    df = pd.DataFrame({"Total Units": [1, 2], "Total Unit": [3, 4], "Cost": [5, 6]})
    fid = owui.upload("alice", "units.csv", df=df)

    def total_unitz():
        return call(tool, "analyze_spreadsheet", ALICE, operation="aggregate", value_column="Total Unitz", file_id=fid)

    assert total_unitz()["status"] == "needs_clarification"  # both candidates score high and close
    tool.valves.ambiguity_min_gap = 0.01
    assert total_unitz()["result"]["value_column"] == "Total Unit"


def test_expression_columns_are_normalized_but_never_fuzzy(engine):
    df = pd.DataFrame({"الإيرادات": [1, 2], "Order Date": ["2025-01-01", "2025-01-02"]})
    assert list(engine._SafeExpr(df).eval("col('الايرادات') * 2")) == [2, 4]
    assert list(engine._SafeExpr(df).eval("`order date` == '2025-01-01'")) == [True, False]
    for expr in ("col('الربح') > 0", "`Order Dat` == 'x'"):
        with pytest.raises(engine._ExprError, match="unknown column"):
            engine._SafeExpr(df).eval(expr)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"operation": "pivot", "value_column": "Units", "group_by": "Territory", "columns": ["Product"]},
        {"operation": "pivot", "value_column": "Units", "group_by": "Region", "columns": ["Prodcts and more"]},
        {"operation": "time_series", "value_column": "Units", "date_column": "Shipped On"},
        {"operation": "describe", "columns": ["Units", "Nope"]},
        {"operation": "duplicates", "subset": ["Order ID", "Nope"]},
        {"operation": "value_counts", "value_column": "Nope"},
    ],
)
def test_every_column_argument_is_resolved_or_questioned(tool, owui, sales_df, kwargs):
    fid = owui.upload("alice", "sales.csv", df=sales_df)
    res = call(tool, "analyze_spreadsheet", ALICE, file_id=fid, **kwargs)
    assert res["status"] == "needs_clarification", res
    assert res["columns_available"] == list(sales_df.columns)


def test_resolved_names_are_used_by_pivot_and_time_series(tool, owui, sales_df):
    fid = owui.upload("alice", "sales.csv", df=sales_df)
    pv = call(
        tool,
        "analyze_spreadsheet",
        ALICE,
        operation="pivot",
        value_column="units",
        group_by="region",
        columns=["product"],
        file_id=fid,
    )
    assert pv["status"] == "ok" and pv["provenance"]["columns_used"] == {
        "value": "Units",
        "rows": "Region",
        "cols": "Product",
    }
    ts = call(
        tool,
        "analyze_spreadsheet",
        ALICE,
        operation="time_series",
        value_column="Units",
        date_column="order_date",
        file_id=fid,
    )
    assert ts["status"] == "ok" and ts["provenance"]["columns_used"]["date"] == "Order Date"
