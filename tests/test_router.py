"""Unit tests for the SheetProof Router filter's output audit helpers."""

from __future__ import annotations

import json

import pytest
from conftest import load_plugin


@pytest.fixture(scope="module")
def router():
    return load_plugin("sheetproof_router.py")


def _tool_output(records):
    return [
        {"type": "function_call", "call_id": "c1", "name": "analyze_spreadsheet"},
        {
            "type": "function_call_output",
            "call_id": "c1",
            "output": json.dumps({"status": "ok", "result": records}, ensure_ascii=False),
        },
    ]


def test_categorical_audit_matches_arabic_headers(router):
    output = _tool_output([{"الاسم": "أحمد", "الحالة": "مكتمل"}, {"الاسم": "سارة", "الحالة": "معلق"}])
    answer = "| الاسم | الحالة |\n| --- | --- |\n| أحمد | معلق |\n| سارة | معلق |"
    flags = router._audit_categorical(output, answer)
    assert len(flags) == 1 and "أحمد" in flags[0] and "مكتمل" in flags[0]


def test_categorical_audit_english(router):
    output = _tool_output([{"Name": "Ann", "Status": "Done"}, {"Name": "Bob", "Status": "Open"}])
    answer = "| Name | Status |\n| --- | --- |\n| Ann | Done |\n| Bob | Done |"
    assert router._audit_categorical(output, answer) == ["Bob · Status='Done' (file 'Open')"]
