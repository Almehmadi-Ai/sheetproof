"""The parsed-file cache: follow-up calls do not re-read the file, entries are bounded, an
entry is never served before the caller passed the file-access check, and cached frames
are never modified by an operation."""

from __future__ import annotations

import asyncio
import json
import os

import pandas as pd
import pytest
from conftest import ALICE, BOB, call


@pytest.fixture
def reads(plugin, monkeypatch):
    """Count real parses of files by the tool."""
    counter = {"n": 0}
    real = plugin._read

    def counting_read(*args, **kwargs):
        counter["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(plugin, "_read", counting_read)
    return counter


def _sum(tool, fid, user=ALICE, **kwargs):
    return call(tool, "analyze_spreadsheet", user, operation="aggregate", value_column="Units", file_id=fid, **kwargs)


def test_follow_up_calls_reuse_the_parsed_file(tool, sales_id, reads):
    first = _sum(tool, sales_id)
    second = _sum(tool, sales_id)
    call(tool, "inspect_spreadsheet", ALICE, file_id=sales_id)
    assert first["result"] == second["result"] and reads["n"] == 1


def test_parsed_measure_columns_are_memoized(plugin, tool, sales_id, monkeypatch):
    call(tool, "analyze_spreadsheet", ALICE, operation="aggregate", value_column="Revenue", file_id=sales_id)
    calls = {"n": 0}
    real = plugin._parse_numbers

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(plugin, "_parse_numbers", counting)
    res = call(tool, "analyze_spreadsheet", ALICE, operation="aggregate", value_column="Revenue", file_id=sales_id)
    assert res["result"]["sum"] == 12705.5 and calls["n"] == 0


def test_a_changed_file_is_read_again(tool, owui, sales_id, reads):
    assert _sum(tool, sales_id)["result"]["sum"] == 127
    path = owui.files.rows[sales_id].path
    pd.DataFrame({"Units": [1, 2, 3]}).to_csv(path, index=False)
    os.utime(path, ns=(os.stat(path).st_atime_ns, os.stat(path).st_mtime_ns + 10**9))
    assert _sum(tool, sales_id)["result"]["sum"] == 6 and reads["n"] == 2


def test_parse_options_are_part_of_the_key(tool, sales_id, reads):
    _sum(tool, sales_id)
    tool.valves.decimal_separator = ","
    _sum(tool, sales_id)
    tool.valves.max_cells = 10_000
    _sum(tool, sales_id)
    _sum(tool, sales_id, sheet="Sheet1")
    assert reads["n"] == 4


def test_cache_is_never_consulted_for_an_unauthorized_caller(tool, owui, sales_id, reads):
    assert _sum(tool, sales_id, user=ALICE)["status"] == "ok"
    assert len(tool._cache) == 1
    denied = _sum(tool, sales_id, user=BOB)
    assert denied == {"error": "File not found, or you do not have access to it."}
    owui.grant_read(sales_id, "bob")  # once Open WebUI lets Bob read the file, he may use the entry
    assert _sum(tool, sales_id, user=BOB)["status"] == "ok" and reads["n"] == 1


def test_entry_count_is_bounded(tool, owui, reads):
    tool.valves.cache_max_entries = 2
    ids = [owui.upload("alice", f"f{i}.csv", df=pd.DataFrame({"Units": [i, i]})) for i in range(3)]
    for fid in ids:
        _sum(tool, fid)
    assert len(tool._cache) == 2
    _sum(tool, ids[0])  # evicted (least recently used) -> read again
    assert reads["n"] == 4


def test_memory_budget_and_disabling(tool, sales_id, reads):
    tool.valves.cache_max_mb = 0.000001
    _sum(tool, sales_id)
    _sum(tool, sales_id)
    assert len(tool._cache) == 0 and reads["n"] == 2
    tool.valves.cache_max_mb, tool.valves.cache_max_entries = 512, 0
    _sum(tool, sales_id)
    assert len(tool._cache) == 0 and reads["n"] == 3


def test_operations_never_modify_the_cached_frame(tool, owui, sales_id):
    _sum(tool, sales_id)
    cached = next(iter(tool._cache._entries.values()))["df"]
    snapshot = cached.copy(deep=True)
    ops = [
        {"operation": "overview"},
        {"operation": "group_aggregate", "value_column": "Revenue", "group_by": "Region"},
        {"operation": "top_n", "value_column": "Units", "group_by": "Product", "n": 2},
        {"operation": "pivot", "value_column": "Units", "group_by": "Region", "columns": ["Product"]},
        {"operation": "value_counts", "value_column": "Region"},
        {"operation": "describe"},
        {"operation": "time_series", "value_column": "Units", "date_column": "Order Date"},
        {"operation": "anomalies", "value_column": "Units"},
        {"operation": "duplicates", "subset": ["Region"]},
        {"operation": "missing"},
        {"operation": "compare", "value_column": "Units", "group_by": "Region"},
        {"operation": "aggregate", "value_column": "Units", "filters": "Region == 'North'"},
        {
            "operation": "transform",
            "pipeline": [
                {"op": "derive", "name": "Units", "expr": "Units * 100"},
                {"op": "window", "fn": "cumsum", "value": "Units", "name": "Region"},
                {"op": "sort", "by": "Units", "dir": "desc"},
            ],
        },
    ]
    for op in ops:
        assert call(tool, "analyze_spreadsheet", ALICE, file_id=sales_id, **op)["status"] == "ok", op
    pd.testing.assert_frame_equal(cached, snapshot)


def test_concurrent_calls_agree(tool, sales_id):
    async def run_all():
        jobs = [
            tool.analyze_spreadsheet(operation="aggregate", value_column="Units", file_id=sales_id, __user__=ALICE)
            for _ in range(8)
        ]
        return await asyncio.gather(*jobs)

    results = {json.loads(r)["result"]["sum"] for r in asyncio.run(run_all())}
    assert results == {127.0}
