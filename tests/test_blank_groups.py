"""Rows whose group value is blank must stay in grouped results as an explicit "(blank)"
group, so per-group figures always reconcile with whole-column totals."""

from __future__ import annotations

import pandas as pd
from conftest import ALICE, call


def test_grand_total_reconciles_with_aggregate_sum(tool, sample_id):
    """One sample order has no region; without a (blank) group grand_total would be 1,709,959.50."""
    grouped = call(
        tool,
        "analyze_spreadsheet",
        ALICE,
        operation="group_aggregate",
        value_column="Revenue",
        group_by="Region",
        file_id=sample_id,
    )
    total = call(
        tool, "analyze_spreadsheet", ALICE, operation="aggregate", value_column="Revenue", agg="sum", file_id=sample_id
    )
    assert total["result"]["sum"] == 1717459.5
    assert grouped["summary"]["grand_total"] == total["result"]["sum"]
    assert round(sum(r["sum"] for r in grouped["result"]), 4) == total["result"]["sum"]
    assert round(sum(r["pct_of_total"] for r in grouped["result"]), 2) == 100.0
    blank = next(r for r in grouped["result"] if r["Region"] == "(blank)")
    assert blank["sum"] == 7500.0 and blank["count"] == 1
    assert grouped["summary"]["blank_group_rows"] == 1


def test_whitespace_only_values_are_blank_and_numeric_keys_work(tool, owui):
    df = pd.DataFrame({"Year": [2024, 2025, None, 2024], "Team": ["A", "  ", None, "B"], "Amount": [1, 2, 3, 4]})
    fid = owui.upload("alice", "t.xlsx", df=df)
    by_team = call(
        tool,
        "analyze_spreadsheet",
        ALICE,
        operation="group_aggregate",
        value_column="Amount",
        group_by="Team",
        file_id=fid,
    )
    assert {r["Team"]: r["sum"] for r in by_team["result"]} == {"A": 1.0, "(blank)": 5.0, "B": 4.0}
    by_year = call(
        tool,
        "analyze_spreadsheet",
        ALICE,
        operation="group_aggregate",
        value_column="Amount",
        group_by="Year",
        file_id=fid,
    )
    assert by_year["status"] == "ok" and by_year["summary"]["grand_total"] == 10.0


def test_pivot_keeps_blank_rows_and_columns(tool, sample_id):
    pv = call(
        tool,
        "analyze_spreadsheet",
        ALICE,
        operation="pivot",
        value_column="Revenue",
        group_by="Region",
        columns=["Category"],
        file_id=sample_id,
    )
    cells = sum(v for r in pv["result"] for k, v in r.items() if k != "Region")
    assert round(cells, 2) == 1717459.5
    assert "(blank)" in [r["Region"] for r in pv["result"]]


def test_segment_compare_and_anomaly_breakdown_label_blanks(tool, sample_id):
    cmp_ = call(
        tool,
        "analyze_spreadsheet",
        ALICE,
        operation="compare",
        value_column="Revenue",
        group_by="Region",
        file_id=sample_id,
    )
    assert "(blank)" in [r["Region"] for r in cmp_["result"]]
    anomalies = call(
        tool,
        "analyze_spreadsheet",
        ALICE,
        operation="anomalies",
        value_column="Units",
        method="zscore",
        threshold=0.0,
        file_id=sample_id,
    )
    assert anomalies["result"]["breakdown"]["Region"]["(blank)"] == 1


def test_value_counts_report_blank_cells(tool, sample_id):
    out = call(tool, "analyze_spreadsheet", ALICE, operation="value_counts", value_column="Region", file_id=sample_id)
    res = out["result"]
    assert res["blank_cells"] == 1
    assert res["total"] + res["blank_cells"] == out["provenance"]["rows_total"] == 228
