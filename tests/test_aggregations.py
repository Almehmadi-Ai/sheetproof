"""Aggregation names, resample frequencies and anomaly methods come from the model. Each is
checked against an explicit allow-list; nothing is resolved with getattr or handed to
pandas' string dispatch unchecked, and an unknown value is an error, never a silent sum."""

from __future__ import annotations

import pandas as pd
import pytest
from conftest import ALICE, call, revenue_numbers


@pytest.mark.parametrize(
    "spec",
    [
        "to_json:Units",
        "__class__:Units",
        "apply:Units",
        "pipe:Units",
        "to_csv:Units",
        "__init__:Units",
        "describe:Units",
        "ohlc:Units",
    ],
)
@pytest.mark.parametrize("by", [[], ["Region"]])
def test_group_rejects_non_allow_listed_functions(engine, sales_df, spec, by):
    with pytest.raises(engine._ExprError, match="unknown aggregation"):
        engine._do_group(sales_df, by, {"out": spec})


def test_group_allow_listed_functions_match_pandas(engine, sales_df):
    units = sales_df["Units"]
    res = engine._do_group(sales_df, [], {f: f"{f}:Units" for f in engine._AGG_FUNCS})
    for f in engine._AGG_FUNCS:
        assert res.iloc[0][f] == pytest.approx(getattr(units, f)())


def test_group_count_of_a_text_column_counts_values(engine, sales_df):
    res = engine._do_group(sales_df, [], {"n": "count:Region"})
    assert res.iloc[0]["n"] == sales_df["Region"].notna().sum()  # every value, not only those that parse as numbers


def test_transform_pivot_rejects_unknown_agg(tool, sales_id):
    res = call(
        tool,
        "analyze_spreadsheet",
        ALICE,
        operation="transform",
        file_id=sales_id,
        pipeline=[{"op": "pivot", "index": "Region", "columns": "Product", "values": "Units", "agg": "to_json"}],
    )
    assert res["status"] == "error" and "unknown aggregation" in res["reason"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"operation": "aggregate", "value_column": "Units", "agg": "average"},
        {"operation": "aggregate", "value_column": "Units", "aggs": ["sum", "to_json"]},
        {"operation": "pivot", "value_column": "Units", "group_by": "Region", "columns": ["Product"], "agg": "to_json"},
        {"operation": "top_n", "value_column": "Units", "group_by": "Region", "agg": "__class__"},
    ],
)
def test_unknown_aggregations_are_errors_not_silent_sums(tool, sales_id, kwargs):
    res = call(tool, "analyze_spreadsheet", ALICE, file_id=sales_id, **kwargs)
    assert res["status"] == "error"
    assert res["supported"] == ["sum", "mean", "count", "min", "max", "median", "std", "nunique"]


@pytest.mark.parametrize("freq", ["1s", "1ns", "min", "h", "BME", "nonsense"])
def test_unknown_resample_frequencies_are_rejected(tool, sales_id, freq):
    for op in ("time_series", "compare"):
        res = call(
            tool,
            "analyze_spreadsheet",
            ALICE,
            operation=op,
            value_column="Units",
            date_column="Order Date",
            freq=freq,
            file_id=sales_id,
        )
        assert res["status"] == "error" and "Unknown freq" in res["reason"], res


def test_time_series_uses_the_requested_agg(tool, sales_id, sales_df):
    res = call(
        tool,
        "analyze_spreadsheet",
        ALICE,
        operation="time_series",
        value_column="Units",
        date_column="Order Date",
        freq="month",
        agg="max",
        file_id=sales_id,
    )
    want = sales_df.set_index(pd.to_datetime(sales_df["Order Date"]))["Units"].resample("ME").max()
    assert res["result"] == {str(k.date()): float(v) for k, v in want.items()}


def test_top_n_ranks_by_the_requested_agg(tool, sales_id, sales_df):
    res = call(
        tool,
        "analyze_spreadsheet",
        ALICE,
        operation="top_n",
        value_column="Revenue",
        group_by="Product",
        agg="mean",
        n=2,
        file_id=sales_id,
    )
    want = revenue_numbers(sales_df).groupby(sales_df["Product"]).mean().sort_values(ascending=False).head(2)
    assert [r["Product"] for r in res["result"]] == list(want.index)
    assert res["provenance"]["ranked_by"] == "mean"


def test_unknown_anomaly_method_is_rejected(tool, sales_id):
    res = call(
        tool,
        "analyze_spreadsheet",
        ALICE,
        operation="anomalies",
        value_column="Units",
        method="isolation_forest",
        file_id=sales_id,
    )
    assert res["status"] == "error" and res["supported"] == ["iqr", "zscore", "pct_above_mean"]
