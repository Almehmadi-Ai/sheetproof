"""Numbers stored as text: a cell is a number only if the whole cell is one, and the decimal
separator is inferred per column from values that can only be read one way. Values that
read both ways ("1,234") follow the column, then the file, then the decimal_separator
valve, and the choice is reported in provenance."""

from __future__ import annotations

import math

import pandas as pd
import pytest
from conftest import ALICE, call


@pytest.mark.parametrize(
    ("cell", "dot", "comma"),
    [
        ("1234", 1234.0, 1234.0),
        ("1,234.56", 1234.56, None),  # US
        ("1.234,56", None, 1234.56),  # European
        ("1,5", None, 1.5),
        ("0.25", 0.25, None),
        ("0,123", None, 0.123),
        ("1,234", 1234.0, 1.234),  # ambiguous
        ("1.234", 1.234, 1234.0),  # ambiguous
        ("1,234,567", 1234567.0, None),
        ("1.234.567", None, 1234567.0),
        ("1,23,456.5", 123456.5, None),  # Indian grouping
        ("1'234.50", 1234.5, None),  # Swiss apostrophe
        ("1’234,50", None, 1234.5),  # typographic apostrophe
        ("1 234,56", None, 1234.56),  # space thousands (French)
        ("1 234,56", None, 1234.56),  # no-break space
        ("$1,000.00", 1000.0, None),
        ("-$1,000.00", -1000.0, None),
        ("($1,000.00)", -1000.0, None),  # accounting negative
        ("€ 1.234,56", None, 1234.56),
        ("1.234,56 €", None, 1234.56),
        ("SAR 1,500.75", 1500.75, None),
        ("12%", 12.0, 12.0),  # percent keeps the shown value
        ("−7", -7.0, -7.0),  # Unicode minus
        ("1e3", 1000.0, 1000.0),
        ("2.5E-3", 0.0025, None),
        ("١٢٣", 123.0, 123.0),  # Arabic-Indic digits
        ("١٢٫٥", 12.5, 12.5),  # Arabic decimal separator
        ("١٬٢٣٤٫٥", 1234.5, 1234.5),  # Arabic thousands + decimal separators
        ("۱۲۳", 123.0, 123.0),  # Persian digits
    ],
)
def test_cell_readings(engine, cell, dot, comma):
    assert engine._parse_number_text(cell) == (dot, comma)


@pytest.mark.parametrize(
    "cell",
    [
        "P0",
        "SKU-100",
        "A1B2",
        "12-34",
        "2025-01-05",
        "1/2",
        "--5",
        "5-",
        "1,2,3",
        "1,234,56",
        "12 34",
        "inf",
        "-inf",
        "nan",
        "NaN",
        "1e400",
        "0x1A",
        "1_000",
        "",
        " ",
        "%",
        "$",
        "USDA 5",
        "N/A",
        "TRUE",
    ],
)
def test_non_numbers(engine, cell):
    assert engine._parse_number_text(cell) == (None, None)


def _parse(engine, values, decimal=None):
    return engine._parse_numbers(pd.Series(values, dtype=object), decimal)


def test_european_column(engine):
    values, info = _parse(engine, ["1.234,56", "2.500,25", "3.000,00"])
    assert round(values.sum(), 2) == 6734.81
    assert info["decimal_separator"] == "," and info["decided_by"] == "column values"


def test_us_column(engine):
    values, info = _parse(engine, ["1,234.56", "2,500.25", "3,000.00"])
    assert round(values.sum(), 2) == 6734.81
    assert info["decimal_separator"] == "."


def test_ambiguous_values_follow_the_column(engine):
    assert list(_parse(engine, ["1,000", "2,5"])[0]) == [1.0, 2.5]
    assert list(_parse(engine, ["1,000", "2.5"])[0]) == [1000.0, 2.5]
    assert list(_parse(engine, ["1.000", "2,5"])[0]) == [1000.0, 2.5]


def test_fully_ambiguous_column_uses_the_fallback(engine):
    values, info = _parse(engine, ["1,000", "2,500"])
    assert list(values) == [1000.0, 2500.0]
    assert info == {"decimal_separator": ".", "decided_by": "fallback", "ambiguous_values": 2}
    assert list(_parse(engine, ["1,000", "2,500"], decimal=",")[0]) == [1.0, 2.5]


def test_mixed_object_column_and_blanks(engine):
    values, info = _parse(engine, [100, "N/A", 200.5, "1.234,5", None, ""])
    assert values.iloc[[0, 2, 3]].tolist() == [100.0, 200.5, 1234.5]
    assert all(math.isnan(v) for v in values.iloc[[1, 4, 5]])
    assert info["non_numeric_cells"] == 2  # "N/A" and "" (None is blank)


def test_numeric_dtype_is_untouched(engine):
    values, info = engine._parse_numbers(pd.Series([1.234, 2.5]))
    assert list(values) == [1.234, 2.5] and info == {}


def test_identifier_like_text_is_not_numeric(engine):
    codes = pd.Series([f"P{i}" for i in range(30)])
    skus = pd.Series([f"SKU-{100 + i}" for i in range(30)])
    assert engine._coerce_numeric(codes).isna().all()
    assert engine._coerce_numeric(skus).isna().all()
    roles = engine._classify(pd.DataFrame({"Code": codes, "SKU": skus, "Qty": range(30)}))
    assert roles["Code"]["role"] != "measure" and roles["SKU"]["role"] != "measure"


def test_file_level_evidence(engine):
    df = pd.DataFrame({"a": ["1.234,56", "9,5"], "b": ["1.000", "2.000"], "c": [1, 2]})
    assert engine._decimal_evidence(df) == ","
    assert engine._decimal_evidence(pd.DataFrame({"a": ["1,234.5"], "b": ["7"]})) == "."
    assert engine._decimal_evidence(pd.DataFrame({"a": ["1,000"], "b": ["x"]})) is None


# --------------------------------------------------------------------------- #
# End to end through the tool
# --------------------------------------------------------------------------- #
def test_european_csv_sums_correctly(tool, owui):
    fid = owui.upload("alice", "eu.csv", "Item;Amount\nA;1.234,56\nB;2.500,25\nC;3.000,00\n")
    res = call(tool, "analyze_spreadsheet", ALICE, operation="aggregate", value_column="Amount", file_id=fid)
    assert res["result"]["sum"] == 6734.81
    assert res["provenance"]["number_format"]["Amount"] == {
        "decimal_separator": ",",
        "decided_by": "column values",
        "ambiguous_values": 0,
    }


def test_ambiguous_column_follows_the_rest_of_the_file(tool, owui):
    text = "Item;Price;Fee\nA;1.234,56;€1.000\nB;99,90;€2.500\n"
    fid = owui.upload("alice", "eu_fee.csv", text)
    res = call(tool, "analyze_spreadsheet", ALICE, operation="aggregate", value_column="Fee", file_id=fid)
    assert res["result"]["sum"] == 3500.0
    assert res["provenance"]["number_format"]["Fee"]["decided_by"] == "other values in the file"


def test_decimal_separator_valve(tool, owui):
    fid = owui.upload("alice", "amb.csv", 'Item,Weight\nA,"1,500"\nB,"2,250"\n')
    res = call(tool, "analyze_spreadsheet", ALICE, operation="aggregate", value_column="Weight", file_id=fid)
    assert res["result"]["sum"] == 3750.0
    assert res["provenance"]["number_format"]["Weight"]["decided_by"].startswith("default")
    tool.valves.decimal_separator = ","
    res = call(tool, "analyze_spreadsheet", ALICE, operation="aggregate", value_column="Weight", file_id=fid)
    assert res["result"]["sum"] == 3.75
    assert res["provenance"]["number_format"]["Weight"] == {
        "decimal_separator": ",",
        "decided_by": "decimal_separator valve",
        "ambiguous_values": 2,
    }


def test_swiss_and_arabic_columns(tool, owui):
    df = pd.DataFrame({"Swiss": ["1'234.50", "2'000.25"], "عربي": ["١٬٢٣٤٫٥", "١٠٫٢٥"]})
    fid = owui.upload("alice", "mixed.xlsx", df=df)
    swiss = call(tool, "analyze_spreadsheet", ALICE, operation="aggregate", value_column="Swiss", file_id=fid)
    arabic = call(tool, "analyze_spreadsheet", ALICE, operation="aggregate", value_column="عربي", file_id=fid)
    assert swiss["result"]["sum"] == 3234.75
    assert arabic["result"]["sum"] == 1244.75


def test_text_column_is_not_summed(tool, owui):
    df = pd.DataFrame({"Code": [f"P{i}" for i in range(30)], "Qty": [i % 7 for i in range(30)]})
    fid = owui.upload("alice", "codes.csv", df=df)
    res = call(tool, "analyze_spreadsheet", ALICE, operation="aggregate", value_column="Code", file_id=fid)
    assert res["status"] == "needs_clarification"
    assert "not numeric" in res["reason"] and res["candidates"] == ["Qty"]


def test_inspect_reports_the_number_format(tool, owui):
    fid = owui.upload("alice", "eu.csv", "Item;Amount\nA;1.234,56\n")
    res = call(tool, "inspect_spreadsheet", ALICE, file_id=fid)
    assert res["number_format"] == {"decimal_separator_fallback": ",", "decided_by": "other values in the file"}
