"""One realistic round trip, wired the way Open WebUI 0.12.2 wires it (main.py and
utils/middleware.py): the router's inlet rewrites the chat request, the tool is called with
the extra parameters the middleware injects, and the router's outlet audits the answer
built from the assistant message's `output` blocks."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from conftest import ALICE, load_plugin


@pytest.fixture
def router():
    return load_plugin("sheetproof_router.py").Filter()


def _assistant_message(tool_json: str, answer: str) -> dict:
    """The assistant message as the 0.12.2 outlet receives it (native function calling)."""
    output = [
        {
            "type": "function_call",
            "id": "call_1",
            "call_id": "call_1",
            "name": "analyze_spreadsheet",
            "arguments": json.dumps({"operation": "group_aggregate", "value_column": "Revenue", "group_by": "Region"}),
            "status": "completed",
        },
        {
            "type": "function_call_output",
            "id": "fco_1",
            "call_id": "call_1",
            "output": [{"type": "input_text", "text": tool_json}],
            "status": "completed",
        },
        {
            "type": "message",
            "id": "msg_1",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": answer}],
        },
    ]
    return {"id": "a1", "role": "assistant", "content": answer, "output": output}


def test_round_trip_with_output_audit(sample_id, tool, router, caplog):
    fid = sample_id
    attachment = {
        "type": "file",
        "id": fid,
        "name": "sample_sales.csv",
        "url": f"/api/v1/files/{fid}",
        "status": "uploaded",
        "size": 21_000,
    }
    form_data = {
        "model": "any-model",
        "stream": True,
        "params": {},
        "tool_ids": [],
        "messages": [{"role": "user", "content": "Total revenue by region, with shares?"}],
        "files": [attachment],
    }
    # main.py copies the client's files list into metadata before the inlet filters run
    metadata = {
        "user_id": "alice",
        "chat_id": "chat-1",
        "message_id": "a1",
        "files": form_data["files"],
        "filter_ids": [],
    }

    body = asyncio.run(router.inlet(form_data, __user__=ALICE, __metadata__=metadata))
    assert body["files"] == []  # retrieval bypass: the spreadsheet never reaches the prompt
    assert body["tool_ids"] == ["sheetproof"] and body["params"]["function_calling"] == "native"
    assert body["messages"][0]["role"] == "system" and fid in body["messages"][0]["content"]
    assert metadata["analyst_file_ids"] == [fid]

    # utils/middleware.py: __files__ = metadata['files'], plus __metadata__ and __user__
    tool_json = asyncio.run(
        tool.analyze_spreadsheet(
            operation="group_aggregate",
            value_column="Revenue",
            group_by="Region",
            __files__=metadata["files"],
            __metadata__=metadata,
            __user__=ALICE,
        )
    )
    result = json.loads(tool_json)
    assert result["status"] == "ok"
    grand_total = result["summary"]["grand_total"]
    north = next(r for r in result["result"] if r["Region"] == "North")
    assert grand_total == 1717459.5

    answer = (
        f"Total revenue is {grand_total:,.2f}. North contributed {north['sum']:,.2f} "
        f"({north['pct_of_total']:.2f}% of the total), and revenue grew 37.25% year over year."
    )
    outlet_body = {"model": "any-model", "messages": [form_data["messages"][-1], _assistant_message(tool_json, answer)]}

    with caplog.at_level(logging.WARNING, logger="open_webui.spreadsheet_audit"):
        out = asyncio.run(router.outlet(outlet_body, __user__=ALICE, __metadata__=metadata))
    assert out["messages"][-1]["content"] == answer  # 'log' mode never edits the visible answer
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 1 and "1 unverified figure(s)" in warnings[0] and "37.25%" in warnings[0]

    router.valves.audit_action = "annotate"
    annotated = asyncio.run(router.outlet(outlet_body, __user__=ALICE, __metadata__=metadata))
    note = annotated["messages"][-1]["content"][len(answer) :]
    assert "Unverified figures detected" in note and "37.25%" in note
    assert f"{grand_total:,.2f}" not in note and f"{north['sum']:,.2f}" not in note


def test_other_users_cannot_ride_along_on_the_router_stash(sample_id, tool, router):
    forged = {"type": "file", "id": sample_id, "name": "sample_sales.csv"}
    form_data = {"model": "m", "messages": [{"role": "user", "content": "sum revenue"}], "files": [forged]}
    metadata = {"user_id": "bob", "files": form_data["files"]}
    bob = {**ALICE, "id": "bob", "name": "Bob", "email": "bob@example.com"}
    asyncio.run(router.inlet(form_data, __user__=bob, __metadata__=metadata))
    res = json.loads(
        asyncio.run(
            tool.analyze_spreadsheet(
                operation="aggregate",
                value_column="Revenue",
                __files__=metadata["files"],
                __metadata__=metadata,
                __user__=bob,
            )
        )
    )
    assert res == {"error": "No uploaded spreadsheet found for this message."}
