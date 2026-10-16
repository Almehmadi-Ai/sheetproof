"""Both plugins must stay single, self-contained files that Open WebUI can load: a frontmatter
docstring first, no text that open_webui.utils.plugin.replace_imports would rewrite, no
imports outside the declared requirements, and tool docstrings that become the model's
function specs."""

from __future__ import annotations

import ast
import inspect
import re
import sys

import pytest
from conftest import ROOT, load_plugin

PLUGINS = {"sheetproof.py": "Tools", "sheetproof_router.py": "Filter"}
STDLIB = set(sys.stdlib_module_names)


def _frontmatter(text: str) -> dict:
    """open_webui.utils.plugin.extract_frontmatter (0.12.2)."""
    lines = text.splitlines()
    assert lines[0].strip() == '"""'
    fm = {}
    for line in lines[1:]:
        if '"""' in line:
            break
        m = re.match(r"^\s*([a-z_]+):\s*(.*)\s*$", line, re.IGNORECASE)
        if m:
            fm[m.group(1).strip()] = m.group(2).strip()
    return fm


@pytest.mark.parametrize(("filename", "cls"), PLUGINS.items())
def test_plugin_is_loadable_by_open_webui(filename, cls):
    text = (ROOT / filename).read_text(encoding="utf-8")
    fm = _frontmatter(text)
    assert {"title", "version", "required_open_webui_version", "description"} <= fm.keys()
    for rewritten in ("from utils", "from apps", "from main", "from config"):
        assert rewritten not in text, f"Open WebUI would rewrite '{rewritten}'"
    getattr(load_plugin(filename), cls)()


def test_tool_imports_only_stdlib_declared_requirements_and_open_webui():
    text = (ROOT / "sheetproof.py").read_text(encoding="utf-8")
    declared = {r.strip() for r in _frontmatter(text)["requirements"].split(",")} | {"pydantic"}
    for node in ast.walk(ast.parse(text)):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            top = name.split(".")[0]
            assert top in STDLIB or top in declared or top == "open_webui", name


def test_versions_match():
    versions = {f: _frontmatter((ROOT / f).read_text(encoding="utf-8"))["version"] for f in PLUGINS}
    assert len(set(versions.values())) == 1, versions


def test_tools_offers_the_model_only_the_two_tool_functions():
    """Open WebUI 0.5 and 0.6 offer the model every callable Tools attribute that is not a class
    and not named __like_this__ (later releases also skip _private names)."""
    tools = load_plugin("sheetproof.py").Tools()
    offered = {
        name
        for name in dir(tools)
        if not name.startswith("__") and callable(getattr(tools, name)) and not inspect.isclass(getattr(tools, name))
    }
    assert offered == {"inspect_spreadsheet", "analyze_spreadsheet"}


def test_tool_functions_are_documented_for_the_model():
    tools = load_plugin("sheetproof.py").Tools()
    for name in ("inspect_spreadsheet", "analyze_spreadsheet"):
        fn = getattr(tools, name)
        assert inspect.iscoroutinefunction(fn)
        doc = inspect.getdoc(fn)
        params = [p for p in inspect.signature(fn).parameters if not p.startswith("__")]
        for p in params:
            assert f":param {p}:" in doc, f"{name} does not document {p}"
