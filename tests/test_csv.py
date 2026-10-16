"""CSV/TSV loading: delimiter detection, the same header-row detection the Excel path uses,
and the column types pandas itself would infer."""

from __future__ import annotations

import pandas as pd
import pytest
from conftest import ALICE, ROOT, call


def _read(plugin, tmp_path, text, name="data.csv"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="")
    df, sheets, chosen = plugin._read(str(path), plugin.Tools.Valves())
    assert sheets == ["(csv)"] and chosen == "(csv)"
    return df


def test_banner_rows_are_skipped(plugin, tmp_path):
    """The title line is not the header (read_csv(sep=None) takes it for one)."""
    text = "Monthly Sales Report\nGenerated 2025-07-01\n\nRegion,Units,Revenue\nNorth,10,1000\nSouth,5,500\n"
    df = _read(plugin, tmp_path, text)
    assert list(df.columns) == ["Region", "Units", "Revenue"]
    assert df["Units"].tolist() == [10, 5] and df["Revenue"].sum() == 1500


def test_banner_with_a_label_cell_and_blank_lines(plugin, tmp_path):
    text = 'Report:,Q2 2025,,,\n\n\nOrder,Region,Units,Price,Note\n1,North,3,9.5,"late, again"\n2,South,4,10.0,ok\n'
    df = _read(plugin, tmp_path, text)
    assert list(df.columns) == ["Order", "Region", "Units", "Price", "Note"]
    assert df["Note"].tolist() == ["late, again", "ok"]


def test_quoted_newlines_in_the_banner_count_as_one_record(plugin, tmp_path):
    text = '"Title spanning\ntwo lines"\nA,B,C\n1,2,3\n4,5,6\n'
    df = _read(plugin, tmp_path, text)
    assert list(df.columns) == ["A", "B", "C"] and len(df) == 2


@pytest.mark.parametrize(
    ("delimiter", "name"), [(";", "data.csv"), ("|", "data.csv"), ("\t", "data.tsv"), ("\t", "data.csv")]
)
def test_delimiters(plugin, tmp_path, delimiter, name):
    text = delimiter.join(["Region", "Units", "Note"]) + "\n" + delimiter.join(["North", "10", "a,b"]) + "\n"
    df = _read(plugin, tmp_path, text, name)
    assert list(df.columns) == ["Region", "Units", "Note"] and df["Note"].iloc[0] == "a,b"


def test_semicolon_file_with_comma_decimals(plugin, tmp_path):
    text = "Item;Amount (net, EUR)\nA;1.234,56\nB;2.500,25\nC;3.000,00\n"
    df = _read(plugin, tmp_path, text)
    assert list(df.columns) == ["Item", "Amount (net, EUR)"]


def test_single_column_file(plugin, tmp_path):
    df = _read(plugin, tmp_path, "Name\nAnn\nBob\n")
    assert list(df.columns) == ["Name"] and len(df) == 2


def test_utf8_bom_and_arabic(plugin, tmp_path):
    path = tmp_path / "ar.csv"
    path.write_bytes("﻿المنطقة,الإيرادات\nشمال,100\n".encode())
    df, _, _ = plugin._read(str(path), plugin.Tools.Valves())
    assert list(df.columns) == ["المنطقة", "الإيرادات"] and df["الإيرادات"].tolist() == [100]


def test_types_match_plain_pandas_on_a_regular_file(plugin):
    sample = ROOT / "examples" / "sample_sales.csv"
    ours, _, _ = plugin._read(str(sample), plugin.Tools.Valves())
    pd.testing.assert_frame_equal(ours, pd.read_csv(sample), check_dtype=True)


def test_booleans_and_blanks_keep_pandas_types(plugin, tmp_path):
    df = _read(plugin, tmp_path, "Flag,Maybe,Qty\nTrue,True,1\nFalse,,2\n")
    assert df["Flag"].dtype == bool and df["Flag"].tolist() == [True, False]
    assert df["Maybe"].tolist()[0] is True and pd.isna(df["Maybe"].iloc[1])
    assert df["Qty"].dtype == "int64"


def test_thousands_dots_in_a_comma_decimal_file_stay_text(plugin, tmp_path):
    """pandas alone would read '1.000' as 1.0 even when the file's other numbers use ',' as
    the decimal separator; such a column is kept as text and read as 1000."""
    df = _read(plugin, tmp_path, "Item;Qty;Price\nA;1.000;1.234,56\nB;2.500;99,90\n")
    assert df["Qty"].tolist() == ["1.000", "2.500"]


def test_dot_decimals_in_a_dot_decimal_file_are_numbers(plugin, tmp_path):
    df = _read(plugin, tmp_path, 'Item,Qty,Price\nA,1.000,"1,234.56"\nB,2.500,99.90\n')
    assert df["Qty"].tolist() == [1.0, 2.5]


def test_end_to_end_banner_and_european_quantities(tool, owui):
    text = "Inventaire;;\nGénéré le 01/07/2025;;\n;;\nArticle;Quantité;Prix\nA;1.000;1.234,56\nB;2.500;99,90\n"
    fid = owui.upload("alice", "inventaire.csv", text)
    schema = call(tool, "inspect_spreadsheet", ALICE, file_id=fid)
    assert schema["columns"] == ["Article", "Quantité", "Prix"]
    qty = call(tool, "analyze_spreadsheet", ALICE, operation="aggregate", value_column="Quantité", file_id=fid)
    assert qty["result"]["sum"] == 3500.0
    price = call(tool, "analyze_spreadsheet", ALICE, operation="aggregate", value_column="Prix", file_id=fid)
    assert price["result"]["sum"] == 1334.46
