"""File access must follow Open WebUI's read rule for files (owner, admin, or an explicit
read grant through its access control) for every id the tool is handed: the explicit
file_id argument, the message's attached files, and the router's stash. All three come
from the client request, so none of them is trusted on its own."""

from __future__ import annotations

import sys

import pandas as pd
import pytest
from conftest import ADMIN, ALICE, BOB, call


@pytest.fixture
def alice_file(owui, sales_df):
    return owui.upload("alice", "sales.csv", df=sales_df)


def _attached(fid, name="sales.csv"):
    return [{"type": "file", "id": fid, "name": name}]


def _rows(res):
    return res.get("rows")


# --------------------------------------------------------------------------- #
# Legitimate access
# --------------------------------------------------------------------------- #
def test_owner_by_explicit_id(tool, alice_file):
    assert _rows(call(tool, "inspect_spreadsheet", ALICE, file_id=alice_file)) == 12


def test_owner_by_attached_file(tool, alice_file):
    assert _rows(call(tool, "inspect_spreadsheet", ALICE, __files__=_attached(alice_file))) == 12


def test_owner_by_router_stash(tool, alice_file):
    meta = {"analyst_file_ids": [alice_file]}
    assert _rows(call(tool, "inspect_spreadsheet", ALICE, __metadata__=meta)) == 12


def test_file_shared_through_open_webui_access_control(tool, owui, alice_file):
    owui.grant_read(alice_file, "bob")  # e.g. a knowledge base or shared chat Bob can read
    assert _rows(call(tool, "inspect_spreadsheet", BOB, file_id=alice_file)) == 12
    assert _rows(call(tool, "inspect_spreadsheet", BOB, __files__=_attached(alice_file))) == 12


def test_admin_can_read_like_the_files_api(tool, alice_file):
    assert _rows(call(tool, "inspect_spreadsheet", ADMIN, file_id=alice_file)) == 12


def test_first_readable_attached_spreadsheet_is_used(tool, owui, alice_file):
    bob_file = owui.upload("bob", "bob.csv", df=pd.DataFrame({"x": [1, 2, 3]}))
    res = call(tool, "inspect_spreadsheet", BOB, __files__=_attached(alice_file) + _attached(bob_file, "bob.csv"))
    assert res["rows"] == 3


# --------------------------------------------------------------------------- #
# Cross-user access is denied
# --------------------------------------------------------------------------- #
def test_other_users_file_by_explicit_id_is_denied(tool, alice_file):
    res = call(tool, "analyze_spreadsheet", BOB, operation="overview", file_id=alice_file)
    assert res == {"error": "File not found, or you do not have access to it."}


def test_unknown_id_and_forbidden_id_look_the_same(tool, alice_file):
    unknown = call(tool, "inspect_spreadsheet", BOB, file_id="00000000-0000-4000-8000-000000000000")
    forbidden = call(tool, "inspect_spreadsheet", BOB, file_id=alice_file)
    assert unknown == forbidden  # no existence oracle


def test_forged_attachment_is_denied(tool, alice_file):
    res = call(
        tool, "inspect_spreadsheet", BOB, __files__=_attached(alice_file), __metadata__={"files": _attached(alice_file)}
    )
    assert res == {"error": "No uploaded spreadsheet found for this message."}


def test_forged_router_stash_is_denied(tool, alice_file):
    res = call(tool, "inspect_spreadsheet", BOB, __metadata__={"analyst_file_ids": [alice_file]})
    assert res == {"error": "No uploaded spreadsheet found for this message."}


def test_explicit_id_is_never_swapped_for_an_attached_file(tool, owui, alice_file):
    bob_file = owui.upload("bob", "bob.csv", df=pd.DataFrame({"x": [1]}))
    res = call(tool, "inspect_spreadsheet", BOB, file_id=alice_file, __files__=_attached(bob_file, "bob.csv"))
    assert "error" in res


@pytest.mark.parametrize(
    "bad_id",
    [
        "*",
        "%",
        "?",
        "**/*",
        "../uploads/*",
        "../../etc/passwd",
        "/etc/passwd",
        "C:\\Windows\\win.ini",
        "sales.csv",
        "{fid}_*",
        "{fid}*",
        "{fid}_sales.csv",
        "{fid}/../{fid}",
        "{fid} ",
        "x{fid}",
        "{fid}\x00",
        "' OR '1'='1",
    ],
)
def test_wildcard_and_traversal_ids_are_rejected_before_lookup(tool, owui, alice_file, bad_id):
    bad_id = bad_id.format(fid=alice_file)
    lookups = owui.files.lookups
    res = call(tool, "inspect_spreadsheet", BOB, file_id=bad_id)
    assert "error" in res and "rows" not in res
    # the id is rejected by format (or, if it is a bare valid id after stripping, by authorization)
    if bad_id.strip() != alice_file:
        assert res["error"].startswith("Invalid file_id")
        assert owui.files.lookups == lookups


def test_attached_ids_with_bad_format_are_ignored(tool, owui, alice_file):
    lookups = owui.files.lookups
    res = call(tool, "inspect_spreadsheet", BOB, __files__=_attached("*") + _attached("../" + alice_file))
    assert res == {"error": "No uploaded spreadsheet found for this message."}
    assert owui.files.lookups == lookups


def test_file_on_disk_without_a_record_is_not_reachable(tool, owui, alice_file):
    """A file in the upload directory is only reachable through an authorized file record."""
    orphan_id = "11111111-2222-4333-8444-555555555555"
    (owui.upload_dir / f"{orphan_id}_orphan.csv").write_text("a\n1\n", encoding="utf-8")
    res = call(tool, "inspect_spreadsheet", ALICE, file_id=orphan_id)
    assert res == {"error": "File not found, or you do not have access to it."}


def test_no_user_means_no_access(tool, alice_file):
    assert "error" in call(tool, "inspect_spreadsheet", None, file_id=alice_file)
    assert "error" in call(tool, "inspect_spreadsheet", {}, __files__=_attached(alice_file))


def test_access_check_failure_fails_closed(tool, owui, alice_file, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("database is down")

    monkeypatch.setattr(sys.modules["open_webui.utils.access_control.files"], "has_access_to_file", broken)
    owui.grant_read(alice_file, "bob")
    assert "error" in call(tool, "inspect_spreadsheet", BOB, file_id=alice_file)
    assert _rows(call(tool, "inspect_spreadsheet", ALICE, file_id=alice_file)) == 12  # owner unaffected


# --------------------------------------------------------------------------- #
# Record handling
# --------------------------------------------------------------------------- #
def test_non_spreadsheet_record_is_refused(tool, owui):
    fid = owui.upload("alice", "notes.txt", "hello")
    assert "not an uploaded spreadsheet" in call(tool, "inspect_spreadsheet", ALICE, file_id=fid)["error"]


def test_record_whose_file_is_missing(tool, owui, alice_file):
    owui.files.rows[alice_file].path = str(owui.upload_dir / "gone.csv")
    assert "error" in call(tool, "inspect_spreadsheet", ALICE, file_id=alice_file)


def test_cloud_storage_paths_are_resolved_through_open_webui_storage(tool, owui, alice_file, monkeypatch):
    local = owui.files.rows[alice_file].path
    owui.files.rows[alice_file].path = "s3://bucket/" + alice_file + "_sales.csv"
    storage = sys.modules["open_webui.storage.provider"].Storage
    monkeypatch.setattr(storage, "get_file", lambda p: local if p.startswith("s3://") else p)
    assert _rows(call(tool, "inspect_spreadsheet", ALICE, file_id=alice_file)) == 12


def test_synchronous_file_model_of_older_open_webui(tool, owui, alice_file, monkeypatch):
    rows = owui.files.rows
    monkeypatch.setattr(owui.files, "get_file_by_id", lambda id, db=None: rows.get(id))
    assert _rows(call(tool, "inspect_spreadsheet", ALICE, file_id=alice_file)) == 12
    assert "error" in call(tool, "inspect_spreadsheet", BOB, file_id=alice_file)
