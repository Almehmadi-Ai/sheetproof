"""Every advertised upload format loads: .xlsx, .xlsm, legacy .xls, .csv and .tsv."""

from __future__ import annotations

import pandas as pd
import pytest
from conftest import ALICE, ROOT, call

FIXTURES = ROOT / "tests" / "fixtures"


def test_legacy_xls_with_title_tab_and_banner(tool, owui):
    fid = owui.upload("alice", "legacy.xls", (FIXTURES / "legacy.xls").read_bytes())
    schema = call(tool, "inspect_spreadsheet", ALICE, file_id=fid)
    assert schema["sheets"] == ["Notes", "Sales"] and schema["active_sheet"] == "Sales"
    assert schema["columns"] == ["Region", "Units", "Revenue"] and schema["rows"] == 4
    res = call(
        tool,
        "analyze_spreadsheet",
        ALICE,
        operation="group_aggregate",
        value_column="Revenue",
        group_by="Region",
        file_id=fid,
    )
    assert res["summary"]["grand_total"] == 2500.75
    assert {r["Region"]: r["sum"] for r in res["result"]} == {"North": 1300.5, "(blank)": 700.0, "South": 500.25}


@pytest.mark.parametrize("name", ["data.xlsx", "data.xlsm", "data.csv", "data.tsv"])
def test_modern_formats(tool, owui, sales_df, name):
    fid = owui.upload("alice", name, df=sales_df)
    res = call(tool, "analyze_spreadsheet", ALICE, operation="aggregate", value_column="Units", file_id=fid)
    assert res["result"]["sum"] == sales_df["Units"].sum()


def test_explicit_sheet_selection(tool, owui, tmp_path):
    path = tmp_path / "two.xlsx"
    with pd.ExcelWriter(path) as writer:
        pd.DataFrame({"a": range(10), "b": range(10)}).to_excel(writer, sheet_name="Big", index=False)
        pd.DataFrame({"x": [1, 2]}).to_excel(writer, sheet_name="Small", index=False)
    fid = owui.upload("alice", "two.xlsx", path.read_bytes())
    assert call(tool, "inspect_spreadsheet", ALICE, file_id=fid)["active_sheet"] == "Big"
    small = call(tool, "inspect_spreadsheet", ALICE, file_id=fid, sheet="Small")
    assert small["active_sheet"] == "Small" and small["columns"] == ["x"]
