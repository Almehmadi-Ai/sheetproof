"""Shared test fixtures.

The plugins are loaded the way Open WebUI 0.12.2 loads them (``open_webui.utils.plugin``):
legacy import paths are rewritten, the source is exec'd into a fresh module, and the module
is dropped from ``sys.modules`` again. The parts of ``open_webui`` the tool talks to are
replaced with small stubs that keep the real 0.12.2 shapes: an async ``Files`` table
returning ``FileModel`` records, ``has_access_to_file`` (read grants via knowledge bases,
channels, shared chats ...), ``UserModel`` and the storage provider.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import types
import uuid
from pathlib import Path
from typing import Optional

import pandas as pd
import pytest
from pydantic import BaseModel, ConfigDict

ROOT = Path(__file__).resolve().parents[1]

# open_webui.utils.plugin.replace_imports (0.12.2)
_IMPORT_REWRITES = {
    "from utils": "from open_webui.utils",
    "from apps": "from open_webui.apps",
    "from main": "from open_webui.main",
    "from config": "from open_webui.config",
}


def load_plugin(filename: str) -> types.ModuleType:
    """Load a plugin file like open_webui.utils.plugin.load_tool_module_by_id does."""
    path = ROOT / filename
    content = path.read_text(encoding="utf-8")
    for old, new in _IMPORT_REWRITES.items():
        content = content.replace(old, new)
    name = f"tool_test_{uuid.uuid4().hex}"
    module = types.ModuleType(name)
    sys.modules[name] = module
    try:
        module.__dict__["__file__"] = str(path)
        exec(compile(content, str(path), "exec"), module.__dict__)  # noqa: S102 - mirrors Open WebUI
        return module
    finally:
        sys.modules.pop(name, None)


# --------------------------------------------------------------------------- #
# Stubs with the open_webui 0.12.2 shapes
# --------------------------------------------------------------------------- #
class FileModel(BaseModel):  # open_webui.models.files.FileModel
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    hash: Optional[str] = None
    filename: str
    path: Optional[str] = None
    data: Optional[dict] = None
    meta: Optional[dict] = None
    created_at: Optional[int]
    updated_at: Optional[int]


class UserModel(BaseModel):  # the fields of open_webui.models.users.UserModel the tool relies on
    id: str
    email: str
    role: str = "pending"
    name: str
    last_active_at: int
    updated_at: int


class FilesTable:  # open_webui.models.files.FilesTable (async since 0.7)
    def __init__(self):
        self.rows: dict[str, FileModel] = {}
        self.lookups = 0

    async def get_file_by_id(self, id, db=None):
        self.lookups += 1
        return self.rows.get(id)

    async def get_file_by_id_and_user_id(self, id, user_id, db=None):
        f = self.rows.get(id)
        return f if f is not None and f.user_id == user_id else None


class FakeOpenWebUI:
    """Upload directory + file table + read grants, installed as the open_webui package."""

    def __init__(self, upload_dir: Path):
        self.upload_dir = upload_dir
        self.files = FilesTable()
        self.read_grants: dict[str, set] = {}  # file id -> user ids granted read access

    def upload(self, owner: str, filename: str, content=None, df: Optional[pd.DataFrame] = None) -> str:
        fid = str(uuid.uuid4())
        path = self.upload_dir / f"{fid}_{filename}"
        if df is not None:
            if filename.endswith((".xlsx", ".xlsm")):
                df.to_excel(path, index=False)
            else:
                df.to_csv(path, index=False, sep="\t" if filename.endswith(".tsv") else ",")
        elif isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8", newline="")
        now = int(time.time())
        self.files.rows[fid] = FileModel(
            id=fid,
            user_id=owner,
            filename=filename,
            path=str(path),
            meta={"name": filename},
            created_at=now,
            updated_at=now,
        )
        return fid

    def grant_read(self, file_id: str, user_id: str):
        self.read_grants.setdefault(file_id, set()).add(user_id)

    def modules(self) -> dict:
        files_mod = types.ModuleType("open_webui.models.files")
        files_mod.Files = self.files
        files_mod.FileModel = FileModel
        users_mod = types.ModuleType("open_webui.models.users")
        users_mod.UserModel = UserModel
        access_mod = types.ModuleType("open_webui.utils.access_control.files")

        async def has_access_to_file(file_id, access_type, user, db=None, user_group_ids=None):
            f = await self.files.get_file_by_id(file_id)
            if f is None:
                return False
            if f.user_id == user.id:
                return True
            return access_type == "read" and user.id in self.read_grants.get(file_id, set())

        access_mod.has_access_to_file = has_access_to_file
        storage_mod = types.ModuleType("open_webui.storage.provider")

        class LocalStorageProvider:
            @staticmethod
            def get_file(file_path):
                return file_path

        storage_mod.Storage = LocalStorageProvider()
        config_mod = types.ModuleType("open_webui.config")
        config_mod.UPLOAD_DIR = self.upload_dir
        mods = {
            name: types.ModuleType(name)
            for name in (
                "open_webui",
                "open_webui.models",
                "open_webui.utils",
                "open_webui.utils.access_control",
                "open_webui.storage",
            )
        }
        mods.update(
            {
                "open_webui.models.files": files_mod,
                "open_webui.models.users": users_mod,
                "open_webui.utils.access_control.files": access_mod,
                "open_webui.storage.provider": storage_mod,
                "open_webui.config": config_mod,
            }
        )
        return mods


def make_user(uid: str, role: str = "user") -> dict:
    """The __user__ dict Open WebUI passes to tools (UserModel.model_dump())."""
    return {
        "id": uid,
        "email": f"{uid}@example.com",
        "name": uid.title(),
        "role": role,
        "last_active_at": 0,
        "updated_at": 0,
    }


ALICE = make_user("alice")
BOB = make_user("bob")
ADMIN = make_user("root", role="admin")


@pytest.fixture
def owui(tmp_path, monkeypatch) -> FakeOpenWebUI:
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    fake = FakeOpenWebUI(upload_dir)
    for name, mod in fake.modules().items():
        monkeypatch.setitem(sys.modules, name, mod)
    return fake


@pytest.fixture
def plugin(owui) -> types.ModuleType:
    """A fresh copy of the tool module, so a test can patch its functions."""
    return load_plugin("sheetproof.py")


@pytest.fixture
def tool(plugin):
    return plugin.Tools()


@pytest.fixture(scope="session")
def engine() -> types.ModuleType:
    return load_plugin("sheetproof.py")


def call(tool, fn: str, user: Optional[dict] = ALICE, **kwargs) -> dict:
    """Invoke a tool function as Open WebUI does (extra params as keywords) and decode the JSON."""
    out = asyncio.run(getattr(tool, fn)(__user__=user, **kwargs))
    assert isinstance(out, str)
    return json.loads(out)


@pytest.fixture
def sales_df() -> pd.DataFrame:
    """A small, well-formed table with every kind of column the engine handles."""
    return pd.DataFrame(
        {
            "Order ID": [f"ORD-{i}" for i in range(1, 13)],
            "Region": [
                "North",
                "South",
                "North",
                "West",
                "South",
                None,
                "North",
                "East",
                "West",
                "East",
                "North",
                "South",
            ],
            "Product": [
                "Desk",
                "Chair",
                "Monitor",
                "Desk Lamp",
                "Chair",
                "Desk",
                "Mouse",
                "Monitor",
                "Chair",
                "Desk",
                "Keyboard",
                "Mouse",
            ],
            "Order Date": [
                "2025-01-05",
                "2025-01-20",
                "2025-02-11",
                "2025-02-15",
                "2025-03-01",
                "2025-03-09",
                "2025-03-22",
                "2025-04-02",
                "2025-04-18",
                "2025-05-05",
                "2025-05-30",
                "2025-06-12",
            ],
            "Units": [10, 5, 8, 3, 12, 7, 25, 1, 30, 2, 15, 9],
            "Revenue": [
                "$1,000.00",
                "$500.00",
                "$800.00",
                "$300.00",
                "$1,200.00",
                "$700.00",
                "$2,500.00",
                "$95.50",
                "$3,000.00",
                "$210.00",
                "$1,500.00",
                "$900.00",
            ],
            "Email": [
                "a@x.com",
                "b@x.com",
                "c@x.com",
                "d@x.com",
                "e@x.com",
                "f@x.com",
                "g@x.com",
                "h@x.com",
                "i@x.com",
                "j@x.com",
                "k@x.com",
                "l@x.com",
            ],
        }
    )


@pytest.fixture
def sales_id(owui, sales_df) -> str:
    """sales_df uploaded by Alice as a CSV."""
    return owui.upload("alice", "sales.csv", df=sales_df)


@pytest.fixture
def sample_id(owui) -> str:
    """examples/sample_sales.csv uploaded by Alice."""
    return owui.upload("alice", "sample_sales.csv", (ROOT / "examples" / "sample_sales.csv").read_text("utf-8"))


def revenue_numbers(df: pd.DataFrame) -> pd.Series:
    """Independent parse of the Revenue fixture column (for parity checks)."""
    return df["Revenue"].str.replace(r"[$,]", "", regex=True).astype(float)


def pytest_report_header(config):
    return f"pandas {pd.__version__}; plugins loaded from {os.fspath(ROOT)}"
