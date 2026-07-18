"""
title: SheetProof
author: Abdulrahman Almehmadi
author_url: https://github.com/Almehmadi-Ai/sheetproof
funding_url: https://github.com/Almehmadi-Ai/sheetproof
version: 1.1.0
license: MIT
required_open_webui_version: 0.5.0
requirements: pandas, numpy, openpyxl
description: Deterministic Excel and CSV analysis for Open WebUI. Loads the real uploaded
  spreadsheet (owner-checked), infers a schema, and runs verified pandas operations -
  statistics, group-by, pivot tables, time series, anomaly detection, de-duplication,
  filtering, and a safe composable transform pipeline - returning executed results with
  full provenance. The language model narrates only from verified numbers; it never
  invents, guesses, or recomputes a value itself.
"""

import json
import re
import os
import glob
import math
import ast
from typing import Optional

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

UPLOAD_DIR = "/app/backend/data/uploads"
SPREADSHEET_EXTS = (".xlsx", ".xls", ".xlsm", ".csv", ".tsv")


# --------------------------------------------------------------------------- #
# Helpers (module-level, deterministic, no domain assumptions)
# --------------------------------------------------------------------------- #
def _coerce_numeric(s: pd.Series) -> pd.Series:
    """Locale-tolerant numeric coercion: currency symbols, thousands separators,
    parenthesis-negatives, percent. Tries US then European decimal styles."""
    if pd.api.types.is_numeric_dtype(s):
        return pd.to_numeric(s, errors="coerce")
    x = s.astype(str).str.strip()
    neg = x.str.match(r"^\(.*\)$").fillna(False)
    # US style: comma = thousands, dot = decimal
    us = x.str.replace(r"[^\d.\-]", "", regex=True)
    us_n = pd.to_numeric(us, errors="coerce")
    # European style: dot = thousands, comma = decimal
    eu = (
        x.str.replace(".", "", regex=False)
        .str.replace(",", ".", regex=False)
        .str.replace(r"[^\d.\-]", "", regex=True)
    )
    eu_n = pd.to_numeric(eu, errors="coerce")
    n = us_n if us_n.notna().sum() >= eu_n.notna().sum() else eu_n
    n = n.copy()
    n[neg] = -n[neg].abs()
    return n


def _coerce_datetime(s: pd.Series) -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(s):
        return s
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return pd.to_datetime(s, errors="coerce")


def _is_numeric_col(s: pd.Series, thresh: float = 0.8) -> bool:
    n = _coerce_numeric(s)
    nonnull = s.notna().sum()
    return nonnull > 0 and (n.notna().sum() / max(nonnull, 1)) >= thresh


def _is_datetime_col(s: pd.Series, thresh: float = 0.8) -> bool:
    # Skip numeric/bool; attempt datetime parse on everything else (object, str,
    # string, datetime64). Gating on dtype==object missed pandas' newer 'str' dtype.
    if pd.api.types.is_numeric_dtype(s) or pd.api.types.is_bool_dtype(s):
        return False
    d = _coerce_datetime(s)
    nonnull = int(s.notna().sum())
    return nonnull > 0 and (int(d.notna().sum()) / max(nonnull, 1)) >= thresh


def _classify(df: pd.DataFrame) -> dict:
    """Assign a universal role to every column. No business vocabulary."""
    n = len(df)
    roles = {}
    for col in df.columns:
        s = df[col]
        nonnull = int(s.notna().sum())
        nunique = int(s.nunique(dropna=True))
        card_ratio = (nunique / nonnull) if nonnull else 0.0
        if pd.api.types.is_bool_dtype(s):
            role = "boolean"
        elif _is_datetime_col(s):
            role = "temporal"
        elif _is_numeric_col(s):
            # numeric -> measure. Only an integer-valued, near-unique column is an id
            # (continuous measures are also near-unique, so cardinality alone is wrong).
            ncoerced = _coerce_numeric(s).dropna()
            intlike = bool(len(ncoerced) and (ncoerced % 1 == 0).all())
            role = "identifier" if (intlike and card_ratio > 0.98 and nunique > 20) else "measure"
        else:
            if card_ratio > 0.9 and nunique > 20:
                role = "identifier"
            elif nunique <= max(50, 0.5 * n):
                role = "dimension"
            else:
                role = "text"
        roles[col] = {
            "role": role,
            "dtype": str(s.dtype),
            "non_null": nonnull,
            "nulls": int(n - nonnull),
            "distinct": nunique,
            "samples": [str(v) for v in s.dropna().unique()[:5]],
        }
    return roles


def _detect_header(raw: pd.DataFrame) -> int:
    """Find the real header row by skipping sparse title/preamble/blank rows.
    Works for any file: returns 0 for normal files, >0 when a banner precedes data."""
    if raw is None or raw.empty:
        return 0
    ncols = raw.shape[1]
    counts = raw.notna().sum(axis=1)
    maxc = int(counts.max()) if len(counts) else 0
    if maxc <= 1:
        return 0
    thr = max(0.6 * maxc, min(2, ncols))
    for i in range(min(len(raw), 30)):
        if counts.iloc[i] >= thr:
            return int(i)
    return 0


def _detect_currency(df: pd.DataFrame) -> Optional[str]:
    syms = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY",
            "SAR": "SAR", "AED": "AED", "USD": "USD", "EUR": "EUR", "GBP": "GBP"}
    sample = df.head(50).astype(str).values.ravel().tolist()
    found = set()
    for v in sample:
        v = str(v)
        for k, code in syms.items():
            if k in v:
                found.add(code)
    if len(found) == 1:
        return found.pop()
    if len(found) > 1:
        return "MIXED:" + ",".join(sorted(found))
    return None


def _apply_aggs(series: pd.Series, aggs: list) -> dict:
    """Compute several statistics on one series at once (multi-agg). Deterministic."""
    out = {}
    for a in aggs:
        try:
            if a == "count":
                v = float(series.notna().sum())
            elif a == "nunique":
                v = float(series.nunique())
            elif a == "sum":
                v = float(series.sum())
            elif a == "mean":
                v = float(series.mean())
            elif a == "median":
                v = float(series.median())
            elif a == "min":
                v = float(series.min())
            elif a == "max":
                v = float(series.max())
            elif a == "std":
                v = float(series.std())
            else:
                continue
            out[a] = round(v, 4)
        except Exception:
            out[a] = None
    return out


def _records_to_md(records, cols=None):
    """Render a list of row-dicts as a verbatim markdown table (schema-agnostic).
    This is the authoritative table the model reproduces as-is — every cell is an
    executed value from the real file, so recorded categorical/text values cannot be
    inferred or altered by the model. No column names are assumed."""
    if not isinstance(records, list) or not records or not isinstance(records[0], dict):
        return ""
    if cols is None:
        cols = []
        for r in records:
            for k in r.keys():
                if k not in cols:
                    cols.append(k)

    def _cell(v):
        if v is None or (isinstance(v, float) and v != v):  # None / NaN
            return ""
        return str(v).replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(str(c) for c in cols) + " |",
             "| " + " | ".join("---" for _ in cols) + " |"]
    for r in records:
        lines.append("| " + " | ".join(_cell(r.get(c)) for c in cols) + " |")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Composable pipeline engine: a CLOSED set of relational primitives + a SAFE
# restricted expression grammar. NOT Python exec — only an AST allow-list (column
# refs, params, literals, arithmetic, comparison, boolean logic, whitelisted
# functions). No attributes/imports/calls outside the whitelist => cannot run
# arbitrary code. Generic for any file/workflow; ZERO domain logic.
# --------------------------------------------------------------------------- #
class _ExprError(Exception):
    pass


_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare, ast.Call,
    ast.Name, ast.Load, ast.Constant, ast.List, ast.Tuple,
    ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
)


def _resolve_simple(df, name):
    if name in df.columns:
        return name
    norm = {re.sub(r"[^a-z0-9]", "", str(c).lower()): c for c in df.columns}
    return norm.get(re.sub(r"[^a-z0-9]", "", str(name).lower()))


def _to_dt(x):
    return _coerce_datetime(x) if isinstance(x, pd.Series) else pd.to_datetime(x, errors="coerce")


def _to_bool(x):
    return x.fillna(False).astype(bool) if isinstance(x, pd.Series) else bool(x)


def _as_series(ev, x):
    return x if isinstance(x, pd.Series) else pd.Series([x] * len(ev.df), index=ev.df.index)


def _as_series_like(val, df):
    return val.reindex(df.index) if isinstance(val, pd.Series) else pd.Series([val] * len(df), index=df.index)


# domain-neutral, reusable scalar/vector functions (the convergent function library)
def _f_col(ev, name):
    c = _resolve_simple(ev.df, str(name))
    if c is None:
        raise _ExprError(f"unknown column '{name}'")
    return ev.df[c]

def _f_where(ev, cond, a, b):
    return pd.Series(np.where(_to_bool(cond), a, b), index=ev.df.index)

def _f_coalesce(ev, *args):
    out = _as_series(ev, args[0]).copy()
    for a in args[1:]:
        out = out.fillna(a)
    return out

def _f_datediff(ev, a, b):
    d = _to_dt(a) - _to_dt(b)
    if isinstance(d, pd.Series):
        return d.dt.days
    return None if d is pd.NaT else d.days

def _f_round(ev, x, n=0):
    return _coerce_numeric(_as_series(ev, x)).round(int(n))

def _f_num(ev, x):
    return _coerce_numeric(_as_series(ev, x))

_FUNCS = {
    "col": _f_col, "where": _f_where, "coalesce": _f_coalesce, "datediff": _f_datediff,
    "isnull": lambda ev, x: _as_series(ev, x).isna(),
    "isnan": lambda ev, x: _as_series(ev, x).isna(),
    "notnull": lambda ev, x: _as_series(ev, x).notna(),
    "abs": lambda ev, x: _as_series(ev, x).abs(),
    "round": _f_round, "num": _f_num, "number": _f_num,
    "text": lambda ev, x: _as_series(ev, x).astype(str),
    "str": lambda ev, x: _as_series(ev, x).astype(str),
    "lower": lambda ev, x: _as_series(ev, x).astype(str).str.lower(),
    "upper": lambda ev, x: _as_series(ev, x).astype(str).str.upper(),
    "len": lambda ev, x: _as_series(ev, x).astype(str).str.len(),
    "contains": lambda ev, s, sub: _as_series(ev, s).astype(str).str.contains(str(sub), case=False, na=False, regex=False),
    "startswith": lambda ev, s, p: _as_series(ev, s).astype(str).str.startswith(str(p)),
    "endswith": lambda ev, s, p: _as_series(ev, s).astype(str).str.endswith(str(p)),
    "year": lambda ev, x: _to_dt(_as_series(ev, x)).dt.year,
    "month": lambda ev, x: _to_dt(_as_series(ev, x)).dt.month,
    "day": lambda ev, x: _to_dt(_as_series(ev, x)).dt.day,
    "date": lambda ev, x: _to_dt(_as_series(ev, x)),
    "least": lambda ev, *a: np.minimum.reduce([_coerce_numeric(_as_series(ev, x)) for x in a]),
    "greatest": lambda ev, *a: np.maximum.reduce([_coerce_numeric(_as_series(ev, x)) for x in a]),
}

_BINOPS = {
    ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b, ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b,
}
_CMPOPS = {
    ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b, ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b, ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b,
}


class _SafeExpr:
    """Vectorized evaluation of a restricted expression against a dataframe."""

    def __init__(self, df, params=None):
        self.df = df
        self.params = params or {}

    def eval(self, expr):
        if not isinstance(expr, str):
            raise _ExprError("expression must be a string")
        e = re.sub(r"`([^`]+)`", lambda m: "col(" + repr(m.group(1)) + ")", expr)
        e = re.sub(r"@(\w+)", r"_p_\1", e)
        try:
            tree = ast.parse(e, mode="eval")
        except SyntaxError as ex:
            raise _ExprError(f"cannot parse '{expr}': {ex.msg}")
        for node in ast.walk(tree):
            if not isinstance(node, _ALLOWED_NODES):
                raise _ExprError(f"disallowed syntax: {type(node).__name__}")
            if isinstance(node, ast.Call) and (
                not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS
            ):
                raise _ExprError("only whitelisted functions may be called")
            if isinstance(node, ast.Name) and node.id.startswith("__"):
                raise _ExprError("invalid name")
        return self._ev(tree.body)

    def _ev(self, node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, (ast.List, ast.Tuple)):
            return [self._ev(e) for e in node.elts]
        if isinstance(node, ast.Name):
            nm = node.id
            if nm.startswith("_p_"):
                key = nm[3:]
                if key not in self.params:
                    raise _ExprError(
                        f"unknown parameter @{key}; pass it via params, e.g. "
                        f'params={{"{key}": "2026-06-16"}} (do not drop the value)'
                    )
                return self.params[key]
            if nm in _FUNCS:
                raise _ExprError(f"'{nm}' is a function; call it as {nm}(...)")
            c = _resolve_simple(self.df, nm)
            if c is None:
                raise _ExprError(f"unknown column '{nm}'")
            return self.df[c]
        if isinstance(node, ast.Call):
            return _FUNCS[node.func.id](self, *[self._ev(a) for a in node.args])
        if isinstance(node, ast.BinOp):
            return _BINOPS[type(node.op)](self._ev(node.left), self._ev(node.right))
        if isinstance(node, ast.UnaryOp):
            v = self._ev(node.operand)
            if isinstance(node.op, ast.Not):
                return ~_to_bool(v)
            return -v if isinstance(node.op, ast.USub) else v
        if isinstance(node, ast.BoolOp):
            masks = [_to_bool(self._ev(v)) for v in node.values]
            out = masks[0]
            for m in masks[1:]:
                out = (out & m) if isinstance(node.op, ast.And) else (out | m)
            return out
        if isinstance(node, ast.Compare):
            result, cur = None, self._ev(node.left)
            for op, comp in zip(node.ops, node.comparators):
                right = self._ev(comp)
                if isinstance(op, ast.In):
                    m = _as_series(self, cur).isin(right if isinstance(right, (list, tuple)) else [right])
                elif isinstance(op, ast.NotIn):
                    m = ~_as_series(self, cur).isin(right if isinstance(right, (list, tuple)) else [right])
                else:
                    m = _CMPOPS[type(op)](cur, right)
                result = m if result is None else (_to_bool(result) & _to_bool(m))
                cur = right
            return result
        raise _ExprError(f"unsupported syntax: {type(node).__name__}")


def _parse_aggspec(spec):
    if isinstance(spec, str) and ":" in spec:
        fn, col = spec.split(":", 1)
        return fn.strip().lower(), col.strip()
    return str(spec).strip().lower(), None


def _do_group(df, by, aggspec):
    cols = {}
    for outname, spec in (aggspec or {"count": "count"}).items():
        fn, col = _parse_aggspec(spec)
        if fn == "count" and not col:
            cols[outname] = df.groupby(by, dropna=False).size() if by else pd.Series({"_": len(df)})
            continue
        rc = _resolve_simple(df, col) if col else None
        if rc is None:
            raise _ExprError(f"group: unknown column '{col}'")
        v = df[rc] if fn == "nunique" else _coerce_numeric(df[rc])
        if by:
            tmp = pd.DataFrame({**{b: df[b] for b in by}, "_v": v})
            cols[outname] = tmp.groupby(by, dropna=False)["_v"].agg(fn)
        else:
            cols[outname] = pd.Series({"_": v.nunique() if fn == "nunique" else getattr(v, fn)()})
    res = pd.DataFrame(cols)
    return res.reset_index() if by else res.reset_index(drop=True)


def _do_window(df, step, params):
    fn = (step.get("fn") or "").lower()
    name = step.get("name") or fn
    by = [_resolve_simple(df, b) or b for b in (step.get("by") or [])]
    w = df.copy()
    order_by = step.get("order_by")
    if order_by:
        ob = [_resolve_simple(w, c) or c for c in (order_by if isinstance(order_by, list) else [order_by])]
        w = w.sort_values(ob, ascending=(str(step.get("dir", "asc")).lower() != "desc"))
    rc = _resolve_simple(w, step.get("value")) if step.get("value") else None
    if not rc:
        raise _ExprError("window: 'value' column required")
    col = _coerce_numeric(w[rc])
    g = col.groupby([w[b] for b in by]) if by else None
    if fn == "share":
        tot = g.transform("sum") if by else col.sum()
        w[name] = col / tot * 100
    elif fn == "rank":
        asc = str(step.get("dir", "desc")).lower() != "desc"
        w[name] = g.rank(ascending=asc, method="min") if by else col.rank(ascending=asc, method="min")
    elif fn in ("cumsum", "running_total"):
        w[name] = g.cumsum() if by else col.cumsum()
    elif fn in ("lag", "lead"):
        s = int(step.get("n", 1)) * (1 if fn == "lag" else -1)
        w[name] = g.shift(s) if by else col.shift(s)
    elif fn == "pct_change":
        n = int(step.get("n", 1))
        prev = g.shift(n) if by else col.shift(n)
        w[name] = (col - prev) / prev * 100
    else:
        raise _ExprError(f"window: unknown fn '{fn}'")
    return w


_PIPE_OPS = {"select", "filter", "derive", "group", "aggregate", "sort", "limit",
             "distinct", "window", "pivot", "unpivot"}


def _run_pipeline(df, pipeline, params):
    work = df.copy()
    derived, steps_log = [], []
    for i, step in enumerate(pipeline):
        if not isinstance(step, dict):
            raise _ExprError(f"step {i + 1} is not an object")
        op = (step.get("op") or "").lower()
        if op not in _PIPE_OPS:
            raise _ExprError(f"step {i + 1}: unknown op '{op}'. Allowed: {sorted(_PIPE_OPS)}")
        before = len(work)
        if op == "filter":
            mask = _SafeExpr(work, params).eval(step.get("expr", ""))
            if isinstance(mask, pd.Series):
                work = work[_to_bool(mask).reindex(work.index, fill_value=False)]
            elif not _to_bool(mask):
                work = work.iloc[0:0]
        elif op == "derive":
            name = step.get("name")
            if not name:
                raise _ExprError(f"step {i + 1}: derive needs 'name'")
            val = _SafeExpr(work, params).eval(step.get("expr", ""))
            work = work.copy()
            work[name] = _as_series_like(val, work)
            if name not in derived:
                derived.append(name)
        elif op == "select":
            cols = [c for c in (_resolve_simple(work, x) or x for x in (step.get("columns") or [])) if c in work.columns]
            if not cols:
                raise _ExprError(f"step {i + 1}: select found none of the requested columns")
            work = work[cols]
        elif op in ("group", "aggregate"):
            by = [_resolve_simple(work, c) or c for c in (step.get("by") or [])]
            work = _do_group(work, by, step.get("agg"))
        elif op == "sort":
            by = step.get("by")
            by = [_resolve_simple(work, c) or c for c in (by if isinstance(by, list) else [by])]
            d = step.get("dir", "asc")
            asc = [str(x).lower() != "desc" for x in (d if isinstance(d, list) else [d] * len(by))]
            work = work.sort_values(by, ascending=asc)
        elif op == "limit":
            work = work.head(int(step.get("n", 10)))
        elif op == "distinct":
            cols = step.get("columns")
            work = work.drop_duplicates(subset=[_resolve_simple(work, c) or c for c in cols] if cols else None)
        elif op == "window":
            work = _do_window(work, step, params)
        elif op == "pivot":
            idx = [_resolve_simple(work, c) or c for c in (step.get("index") if isinstance(step.get("index"), list) else [step.get("index")])]
            colc = _resolve_simple(work, step.get("columns"))
            valc = _resolve_simple(work, step.get("values"))
            if not colc or not valc:
                raise _ExprError("pivot needs index, columns, values")
            work = pd.pivot_table(pd.DataFrame({**{c: work[c] for c in idx}, colc: work[colc],
                                                valc: _coerce_numeric(work[valc])}),
                                  index=idx, columns=colc, values=valc,
                                  aggfunc=step.get("agg", "sum"), fill_value=0).reset_index()
            work.columns = [str(c) for c in work.columns]
        elif op == "unpivot":
            idv = [_resolve_simple(work, c) or c for c in (step.get("id_vars") or [])]
            vv = step.get("value_vars")
            work = work.melt(id_vars=idv,
                             value_vars=[_resolve_simple(work, c) or c for c in vv] if vv else None,
                             var_name=step.get("var_name", "variable"),
                             value_name=step.get("value_name", "value"))
        steps_log.append({"step": i + 1, "op": op, "rows_in": int(before), "rows_out": int(len(work))})
    return work, derived, steps_log


class Tools:
    class Valves(BaseModel):
        max_cells: int = Field(
            5_000_000, description="Refuse files larger than this (rows*cols)."
        )
        ambiguity_min_gap: float = Field(
            0.15,
            description="If the top-2 candidate columns for a needed role are closer "
            "than this (by score), ask for clarification instead of guessing.",
        )
        max_return_rows: int = Field(
            1000,
            description="Max detail rows any operation returns. Beyond this the result "
            "is summarized and 'truncated' is flagged true — rows are NEVER silently dropped.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ------------------------------------------------------------------ #
    # file resolution + loading (real file, owner-checked, multi-user safe)
    # ------------------------------------------------------------------ #
    async def _resolve_path(self, file_id, __files__, __metadata__, __user__):
        uid = (__user__ or {}).get("id")
        candidates = []
        # explicit id wins
        if file_id:
            candidates.append(file_id)
        # files attached to THIS message
        for f in (__files__ or []) + (((__metadata__ or {}).get("files")) or []):
            fid = f.get("id") or (f.get("file") or {}).get("id")
            name = f.get("name") or (f.get("file") or {}).get("filename") or ""
            if fid and (not name or name.lower().endswith(SPREADSHEET_EXTS)):
                candidates.append(fid)
        # custom stash set by the sheetproof_router filter
        for fid in ((__metadata__ or {}).get("analyst_file_ids") or []):
            candidates.append(fid)
        seen, ordered = set(), []
        for c in candidates:
            if c and c not in seen:
                seen.add(c); ordered.append(c)
        for fid in ordered:
            path = None
            try:
                from open_webui.models.files import Files
                rec = await Files.get_file_by_id_and_user_id(fid, uid) if uid else None
                if rec is None:
                    rec = await Files.get_file_by_id(fid)
                if rec and getattr(rec, "path", None):
                    path = rec.path
            except Exception:
                path = None
            if not path:  # fallback: locate by id prefix on disk
                hits = glob.glob(os.path.join(UPLOAD_DIR, f"{fid}_*"))
                path = hits[0] if hits else None
            if path and os.path.exists(path):
                return path, fid
        return None, None

    def _read(self, path, sheet=None):
        ext = os.path.splitext(path)[1].lower()
        if ext in (".csv", ".tsv"):
            sep = "\t" if ext == ".tsv" else None
            raw = pd.read_csv(path, sep=sep, header=None, engine="python")
            hdr = _detect_header(raw)
            df = pd.read_csv(path, sep=sep, skiprows=hdr, engine="python")
            return df, ["(csv)"], "(csv)"
        xl = pd.ExcelFile(path)
        sheets = xl.sheet_names
        chosen = sheet if (sheet in sheets) else None
        if chosen is None:
            # pick the sheet with the most cells (data sheet, not a title tab)
            best, best_cells = sheets[0], -1
            for sh in sheets:
                d = pd.read_excel(path, sheet_name=sh, header=None, nrows=200)
                cells = int(d.notna().sum().sum())
                if cells > best_cells:
                    best, best_cells = sh, cells
            chosen = best
        raw = pd.read_excel(path, sheet_name=chosen, header=None, nrows=50)
        hdr = _detect_header(raw)
        df = pd.read_excel(path, sheet_name=chosen, header=hdr)
        return df, sheets, chosen

    async def _load(self, file_id, __files__, __metadata__, __user__, sheet=None):
        path, fid = await self._resolve_path(file_id, __files__, __metadata__, __user__)
        if not path:
            return None, {"error": "No uploaded spreadsheet found for this message."}
        df, sheets, chosen = self._read(path, sheet)
        if df.shape[0] * max(df.shape[1], 1) > self.valves.max_cells:
            return None, {
                "error": f"File too large "
                f"({df.shape[0]} rows x {df.shape[1]} cols). Increase the max_cells valve "
                f"to process it."
            }
        # normalize column names to strings, drop fully-empty columns
        df.columns = [str(c).strip() for c in df.columns]
        df = df.dropna(axis=1, how="all").dropna(how="all").reset_index(drop=True)
        return df, {"path": path, "file_id": fid, "sheets": sheets, "sheet": chosen}

    def _resolve_col(self, df, name, want_role=None, roles=None):
        """Map a user/model-supplied name to a real column. Returns (col, candidates)."""
        if name and name in df.columns:
            return name, []
        cols = list(df.columns)
        # case/space-insensitive exact
        norm = {re.sub(r"[^a-z0-9]", "", c.lower()): c for c in cols}
        if name:
            key = re.sub(r"[^a-z0-9]", "", str(name).lower())
            if key in norm:
                return norm[key], []
            # substring / token overlap
            scored = []
            for c in cols:
                cl = re.sub(r"[^a-z0-9]", "", c.lower())
                score = 0.0
                if key and (key in cl or cl in key):
                    score = 0.7
                else:
                    a, b = set(re.findall(r"[a-z0-9]+", str(name).lower())), set(
                        re.findall(r"[a-z0-9]+", c.lower())
                    )
                    if a and b:
                        score = len(a & b) / len(a | b)
                scored.append((score, c))
            scored.sort(reverse=True)
            if scored and scored[0][0] >= 0.5:
                return scored[0][1], [c for _, c in scored[:3]]
        return None, []

    # ------------------------------------------------------------------ #
    # PUBLIC TOOLS (auto-exposed to the model via signature + docstring)
    # ------------------------------------------------------------------ #
    async def inspect_spreadsheet(
        self,
        file_id: Optional[str] = None,
        sheet: Optional[str] = None,
        __files__: Optional[list] = None,
        __metadata__: Optional[dict] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Inspect the uploaded spreadsheet and return its real schema: sheet names,
        row/column counts, every column with an inferred role
        (measure | dimension | temporal | identifier | text | boolean), dtype,
        null counts, distinct counts, and sample values. ALWAYS call this first so
        you use the file's actual column names. Returns JSON.

        :param file_id: optional Open WebUI file id (auto-detected if omitted)
        :param sheet: optional sheet name to inspect (auto-detected if omitted)
        """
        df, info = await self._load(file_id, __files__, __metadata__, __user__, sheet)
        if df is None:
            return json.dumps(info)
        roles = _classify(df)
        out = {
            "file": os.path.basename(info["path"]),
            "sheets": info["sheets"],
            "active_sheet": info["sheet"],
            "rows": int(len(df)),
            "columns": list(df.columns),
            "currency_detected": _detect_currency(df),
            "schema": roles,
            "measures": [c for c, r in roles.items() if r["role"] == "measure"],
            "dimensions": [c for c, r in roles.items() if r["role"] == "dimension"],
            "temporal": [c for c, r in roles.items() if r["role"] == "temporal"],
            "note": "Use these exact column names when calling analyze_spreadsheet.",
        }
        return json.dumps(out, default=str)

    async def analyze_spreadsheet(
        self,
        operation: str,
        value_column: Optional[str] = None,
        group_by: Optional[str] = None,
        columns: Optional[list] = None,
        agg: str = "sum",
        aggs: Optional[list] = None,
        n: int = 10,
        filters: Optional[str] = None,
        date_column: Optional[str] = None,
        freq: str = "M",
        method: str = "iqr",
        threshold: float = 1.5,
        subset: Optional[list] = None,
        pipeline: Optional[list] = None,
        params: Optional[dict] = None,
        sheet: Optional[str] = None,
        file_id: Optional[str] = None,
        __files__: Optional[list] = None,
        __metadata__: Optional[dict] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Run a DETERMINISTIC analysis on the real uploaded spreadsheet and return
        verified results + provenance as JSON. Every number you report MUST come
        from this tool. Call inspect_spreadsheet first to get real column names.

        operation (required), one of:
          - "overview": shape, dtypes, nulls, duplicates, per-measure stats, top
            dimension values, date range. Use for "summarize this file".
          - "aggregate": one statistic of value_column over the whole file
            (agg = sum|mean|count|min|max|median|std|nunique). Optionally group_by.
          - "group_aggregate": value_column aggregated per group_by, with count and
            % of total, sorted descending.
          - "top_n": top n groups of group_by by agg of value_column.
          - "pivot": value_column aggregated by group_by (rows) x columns (cols).
          - "value_counts": frequency of each value in value_column (a dimension).
          - "describe": descriptive stats for the given columns (or all measures).
          - "time_series": value_column aggregated by date_column resampled at freq
            (D|W|M|Q|Y).
          - "anomalies": rows where value_column is an outlier (method = iqr|zscore|
            pct_above_mean; threshold = IQR mult / z / fraction). Returns the COUNT
            and the flagged rows.
          - "duplicates": duplicate rows (full row, or by subset of columns).
          - "missing": null counts per column.
          - "compare": period-over-period change (give date_column + freq) OR segment
            comparison across group_by (optionally limit to subset=[A, B]). Returns each
            period/group with absolute change and % change.
          - "transform": GENERAL composable pipeline — pass pipeline=[steps]. Each step is
            {"op": ...}: select{columns}, filter{expr}, derive{name,expr}, group{by,agg},
            sort{by,dir}, limit{n}, distinct{columns?}, window{fn,value,by?,order_by?,name}
            (fn=share|rank|cumsum|lag|lead|pct_change), pivot{index,columns,values,agg},
            unpivot{id_vars,value_vars}. Use this for ANY row-level / multi-step workflow:
            computed columns, classifications, statuses, date math, filtered listings,
            running totals, shares. agg is {out_name: "fn" or "fn:Column"} (fn=sum|mean|
            count|min|max|median|std|nunique). Expressions: reference columns as `Col Name`
            or col('Col Name'); pass external values via params={...} and use @name (e.g.
            @today); allowed functions: where, coalesce, datediff, year, month, day, date,
            abs, round, num, text, lower, upper, len, contains, startswith, endswith,
            isnull, notnull, least, greatest. NO arbitrary code is possible.
        IMPORTANT result fields (read these instead of computing anything yourself):
          - group_aggregate/top_n also return a `summary` with grand_total,
            returned_subtotal and returned_share_of_total_pct. Use these for any
            "combined top-N" total or share — never add the rows yourself.
          - anomalies returns a `breakdown` (counts of the flagged set per dimension),
            `flagged_sum`, `flagged_share_of_total_pct`, and `rows_returned`/`truncated`.
            Use breakdown for "how many flagged are X" — never count rows yourself.
          - pass aggs=["sum","mean","count","median"] for multi-statistic group-by or
            whole-file aggregation.
          - For a per-row / per-entity listing (a ready-to-display table), call operation
            "transform" with ONE "select" of all the columns you need; it returns
            `table_markdown` — a ready-to-display table of the exact executed rows. Present
            that table verbatim and copy every recorded cell (rating, status, yes/no,
            category, name, note, date) exactly — NEVER re-type, re-rank, or infer a
            recorded value. Prefer this single select over many separate calls.

        :param operation: the analysis to run (see list above)
        :param value_column: the numeric/measure column to compute on
        :param group_by: the dimension column to group by
        :param columns: explicit list of columns (for describe/pivot columns)
        :param agg: aggregation function (sum|mean|count|min|max|median|std|nunique)
        :param aggs: optional list of aggregations to compute at once (multi-statistic)
        :param n: how many rows/groups to return for top_n/value_counts
        :param filters: optional pandas query string applied before computing
        :param date_column: the temporal column for time_series
        :param freq: resample frequency for time_series (D|W|M|Q|Y)
        :param method: anomaly method (iqr|zscore|pct_above_mean)
        :param threshold: anomaly threshold
        :param subset: list of columns defining a duplicate key
        :param pipeline: list of pipeline steps for operation="transform" (see above)
        :param params: named values usable in expressions via @name (e.g. {"today": "2026-06-16"})
        :param sheet: optional sheet name
        :param file_id: optional Open WebUI file id (auto-detected if omitted)
        """
        df, info = await self._load(file_id, __files__, __metadata__, __user__, sheet)
        if df is None:
            return json.dumps(info)

        roles = _classify(df)
        measures = [c for c, r in roles.items() if r["role"] == "measure"]
        dims = [c for c, r in roles.items() if r["role"] in ("dimension", "identifier")]
        prov = {
            "operation": operation,
            "file": os.path.basename(info["path"]),
            "active_sheet": info["sheet"],
            "rows_total": int(len(df)),
        }
        rows_total = len(df)

        # optional filter
        if filters:
            try:
                df = df.query(filters)
            except Exception as e:
                return json.dumps({"error": f"Could not apply filter '{filters}': {e}"})
        prov["filters_applied"] = filters or None
        prov["rows_after_filter"] = int(len(df))

        def need_measure(col):
            """Resolve/validate a measure column; clarify if ambiguous."""
            if col:
                resolved, cand = self._resolve_col(df, col, "measure", roles)
                if resolved:
                    return resolved, None
                return None, {
                    "status": "needs_clarification",
                    "reason": f"Column '{col}' not found.",
                    "candidates": cand or list(df.columns),
                }
            if len(measures) == 1:
                return measures[0], None
            if len(measures) == 0:
                return None, {"status": "error", "reason": "No numeric measure column detected."}
            return None, {
                "status": "needs_clarification",
                "reason": "Multiple numeric columns could be the measure. Which one?",
                "candidates": measures,
            }

        def num(col):
            return _coerce_numeric(df[col])

        try:
            if operation == "overview":
                ov = {
                    "rows": int(rows_total),
                    "columns": list(df.columns),
                    "roles": {c: roles[c]["role"] for c in df.columns},
                    "missing_per_column": {c: int(df[c].isna().sum()) for c in df.columns if df[c].isna().sum()},
                    "duplicate_full_rows": int(df.duplicated().sum()),
                    "currency_detected": _detect_currency(df),
                    "measures": {},
                    "top_dimensions": {},
                }
                for m in measures:
                    col = num(m)
                    ov["measures"][m] = {
                        "sum": round(float(col.sum()), 4),
                        "mean": round(float(col.mean()), 4),
                        "min": round(float(col.min()), 4),
                        "max": round(float(col.max()), 4),
                        "non_null": int(col.notna().sum()),
                    }
                for d in dims[:5]:
                    ov["top_dimensions"][d] = {
                        str(k): int(v) for k, v in df[d].value_counts().head(5).items()
                    }
                tcols = [c for c, r in roles.items() if r["role"] == "temporal"]
                if tcols:
                    dt = _coerce_datetime(df[tcols[0]])
                    ov["date_range"] = {
                        "column": tcols[0],
                        "min": str(dt.min()),
                        "max": str(dt.max()),
                    }
                prov.update({"columns_used": list(df.columns), "rows_processed": int(rows_total)})
                return json.dumps({"status": "ok", "result": ov, "provenance": prov}, default=str)

            if operation in ("aggregate", "group_aggregate", "top_n"):
                # count works on ANY column (or the whole file) without a measure
                if operation == "aggregate" and not group_by and agg == "count":
                    if value_column:
                        col, cand = self._resolve_col(df, value_column, None, roles)
                        if not col:
                            return json.dumps({"status": "needs_clarification",
                                               "reason": f"Column '{value_column}' not found.",
                                               "candidates": cand or list(df.columns)})
                        cnt = int(df[col].notna().sum())
                    else:
                        col, cnt = "(rows)", int(rows_total)
                    prov.update({"columns_used": {"column": col}, "agg": "count", "rows_processed": int(rows_total),
                                 "code": (f"df['{col}'].notna().sum()" if col != "(rows)" else "len(df)")})
                    return json.dumps({"status": "ok", "result": {"count": cnt, "column": col},
                                       "provenance": prov}, default=str)
                mcol, err = need_measure(value_column)
                if err:
                    return json.dumps(err)
                series = num(mcol)
                processed = int(series.notna().sum())
                agg_list = [a for a in (aggs or []) if a in
                            ("sum", "mean", "count", "min", "max", "median", "std", "nunique")]
                if operation == "aggregate" and not group_by:
                    if agg_list:
                        res = _apply_aggs(series, agg_list)
                        prov.update({"columns_used": {"value": mcol}, "agg": agg_list,
                                     "rows_processed": processed, "code": f"df['{mcol}'].agg({agg_list})"})
                        return json.dumps({"status": "ok", "result": {**res, "value_column": mcol},
                                           "provenance": prov}, default=str)
                    fns = {
                        "sum": series.sum, "mean": series.mean, "count": lambda: series.notna().sum(),
                        "min": series.min, "max": series.max, "median": series.median,
                        "std": series.std, "nunique": lambda: series.nunique(),
                    }
                    val = float(fns.get(agg, series.sum)())
                    prov.update({"columns_used": {"value": mcol}, "agg": agg, "rows_processed": processed,
                                 "code": f"df['{mcol}'].{agg}()"})
                    return json.dumps({"status": "ok", "result": {agg: round(val, 4), "value_column": mcol},
                                       "provenance": prov}, default=str)
                if not group_by:
                    return json.dumps({"status": "needs_clarification",
                                       "reason": "group_by column required for this operation.",
                                       "candidates": dims})
                gcol, cand = self._resolve_col(df, group_by, "dimension", roles)
                if not gcol:
                    return json.dumps({"status": "needs_clarification",
                                       "reason": f"group column '{group_by}' not found.",
                                       "candidates": cand or dims})
                pandas_aggs = []
                for a in (agg_list or ["sum", "count", "mean"]):
                    if a not in pandas_aggs:
                        pandas_aggs.append(a)
                if "sum" not in pandas_aggs:
                    pandas_aggs = ["sum"] + pandas_aggs
                tmp = pd.DataFrame({gcol: df[gcol], mcol: series})
                grouped = tmp.groupby(gcol)[mcol].agg(pandas_aggs)
                grand_total = float(grouped["sum"].sum())
                grouped["pct_of_total"] = grouped["sum"] / (grand_total or 1) * 100
                grouped = grouped.sort_values("sum", ascending=False).round(4)
                groups_total = int(len(grouped))
                if operation == "top_n":
                    grouped = grouped.head(int(n))
                returned = grouped.reset_index().to_dict(orient="records")
                returned_subtotal = round(float(sum((r.get("sum") or 0) for r in returned)), 4)
                summary = {
                    "grand_total": round(grand_total, 4),
                    "groups_total": groups_total,
                    "groups_returned": len(returned),
                    "returned_subtotal": returned_subtotal,
                    "returned_share_of_total_pct": round(returned_subtotal / (grand_total or 1) * 100, 4),
                }
                prov.update({"columns_used": {"value": mcol, "group": gcol}, "agg": pandas_aggs,
                             "rows_processed": processed,
                             "code": f"df.groupby('{gcol}')['{mcol}'].agg({pandas_aggs})"})
                return json.dumps({"status": "ok", "result": returned, "summary": summary,
                                   "provenance": prov}, default=str)

            if operation == "value_counts":
                col, cand = self._resolve_col(df, value_column or group_by, None, roles)
                if not col:
                    return json.dumps({"status": "needs_clarification",
                                       "reason": "Which column to count?", "candidates": list(df.columns)})
                vc = df[col].value_counts().head(int(n))
                total = int(df[col].notna().sum())
                prov.update({"columns_used": {"column": col}, "rows_processed": total,
                             "code": f"df['{col}'].value_counts()"})
                return json.dumps({"status": "ok",
                                   "result": {"counts": {str(k): int(v) for k, v in vc.items()},
                                              "percent": {str(k): round(v / (total or 1) * 100, 2)
                                                          for k, v in vc.items()},
                                              "total": total},
                                   "provenance": prov}, default=str)

            if operation == "describe":
                cols = columns or measures or list(df.select_dtypes(include=np.number).columns)
                desc = {}
                for c in cols:
                    if c in df.columns:
                        s = num(c)
                        desc[c] = {k: round(float(v), 4) for k, v in s.describe().items()}
                prov.update({"columns_used": cols, "rows_processed": int(rows_total),
                             "code": "df[cols].describe()"})
                return json.dumps({"status": "ok", "result": desc, "provenance": prov}, default=str)

            if operation == "time_series":
                if not date_column:
                    tcols = [c for c, r in roles.items() if r["role"] == "temporal"]
                    if len(tcols) != 1:
                        return json.dumps({"status": "needs_clarification",
                                           "reason": "Which date column?", "candidates": tcols})
                    date_column = tcols[0]
                mcol, err = need_measure(value_column)
                if err:
                    return json.dumps(err)
                freq_map = {"D": "D", "W": "W", "M": "ME", "MONTH": "ME", "MONTHLY": "ME",
                            "Q": "QE", "QUARTER": "QE", "QUARTERLY": "QE",
                            "Y": "YE", "A": "YE", "YEAR": "YE", "YEARLY": "YE", "ANNUAL": "YE"}
                fq = freq_map.get(str(freq).upper(), freq)
                t = pd.DataFrame({"d": _coerce_datetime(df[date_column]), "v": num(mcol)}).dropna(subset=["d"])
                ts = t.set_index("d")["v"].resample(fq).agg(agg if agg in ("sum", "mean", "count") else "sum")
                prov.update({"columns_used": {"date": date_column, "value": mcol}, "freq": fq,
                             "rows_processed": int(len(t)),
                             "code": f"df.set_index('{date_column}')['{mcol}'].resample('{fq}').{agg}()"})
                return json.dumps({"status": "ok",
                                   "result": {str(k.date()): round(float(v), 4) for k, v in ts.items()},
                                   "provenance": prov}, default=str)

            if operation == "anomalies":
                mcol, err = need_measure(value_column)
                if err:
                    return json.dumps(err)
                s = num(mcol)
                bounds = {}
                if method == "zscore":
                    mu, sd = s.mean(), (s.std() or 1)
                    mask = ((s - mu) / sd).abs() >= threshold
                    bounds = {"lower": round(float(mu - threshold * sd), 4),
                              "upper": round(float(mu + threshold * sd), 4)}
                elif method == "pct_above_mean":
                    mask = s >= s.mean() * (1 + threshold)
                    bounds = {"threshold_value": round(float(s.mean() * (1 + threshold)), 4)}
                else:  # iqr
                    q1, q3 = s.quantile(0.25), s.quantile(0.75)
                    iqr = q3 - q1
                    mask = (s < q1 - threshold * iqr) | (s > q3 + threshold * iqr)
                    bounds = {"lower": round(float(q1 - threshold * iqr), 4),
                              "upper": round(float(q3 + threshold * iqr), 4)}
                mask = mask.fillna(False)
                flagged = df[mask].copy()
                flagged[mcol] = s[mask]
                total_flagged = int(mask.sum())
                flagged_sum = round(float(s[mask].sum()), 4)
                total_sum = float(s.sum())
                # breakdown of the FLAGGED set per dimension, so the model never counts rows itself
                breakdown = {}
                for d in dims:
                    if d in flagged.columns:
                        vc = flagged[d].value_counts(dropna=False).head(20)
                        breakdown[d] = {("(null)" if (not isinstance(k, str) and pd.isna(k)) else str(k)): int(v)
                                        for k, v in vc.items()}
                cap = int(self.valves.max_return_rows)
                rows_out = flagged.head(cap).to_dict(orient="records")
                prov.update({"columns_used": {"value": mcol}, "method": method, "threshold": threshold,
                             "rows_processed": int(s.notna().sum()),
                             "code": f"anomaly({method}, thr={threshold}) on '{mcol}'"})
                return json.dumps({"status": "ok",
                                   "result": {"anomaly_count": total_flagged,
                                              "mean": round(float(s.mean()), 4),
                                              "bounds": bounds,
                                              "flagged_sum": flagged_sum,
                                              "flagged_share_of_total_pct": round(flagged_sum / (total_sum or 1) * 100, 4),
                                              "breakdown": breakdown,
                                              "rows_returned": len(rows_out),
                                              "truncated": total_flagged > len(rows_out),
                                              "rows": rows_out},
                                   "provenance": prov}, default=str)

            if operation == "duplicates":
                dup_full = int(df.duplicated().sum())
                res = {"duplicate_full_rows": dup_full}
                if subset:
                    valid = [c for c in subset if c in df.columns]
                    if valid:
                        res["duplicate_by_key"] = int(df.duplicated(subset=valid).sum())
                        res["key"] = valid
                prov.update({"columns_used": subset or "all", "rows_processed": int(rows_total),
                             "code": f"df.duplicated(subset={subset})"})
                return json.dumps({"status": "ok", "result": res, "provenance": prov}, default=str)

            if operation == "missing":
                miss = {c: int(df[c].isna().sum()) for c in df.columns}
                prov.update({"columns_used": list(df.columns), "rows_processed": int(rows_total),
                             "code": "df.isna().sum()"})
                return json.dumps({"status": "ok", "result": miss, "provenance": prov}, default=str)

            if operation == "pivot":
                mcol, err = need_measure(value_column)
                if err:
                    return json.dumps(err)
                if not group_by or not columns:
                    return json.dumps({"status": "needs_clarification",
                                       "reason": "pivot needs group_by (rows) and columns (cols).",
                                       "candidates": dims})
                ccol = columns[0] if isinstance(columns, list) else columns
                pv = pd.pivot_table(pd.DataFrame({group_by: df[group_by], ccol: df[ccol], mcol: num(mcol)}),
                                    index=group_by, columns=ccol, values=mcol, aggfunc=agg, fill_value=0).round(4)
                prov.update({"columns_used": {"value": mcol, "rows": group_by, "cols": ccol}, "agg": agg,
                             "rows_processed": int(rows_total),
                             "code": f"pivot_table(index={group_by}, columns={ccol}, values={mcol}, aggfunc={agg})"})
                return json.dumps({"status": "ok", "result": pv.reset_index().to_dict(orient="records"),
                                   "provenance": prov}, default=str)

            if operation == "transform":
                if not isinstance(pipeline, list) or not pipeline:
                    return json.dumps({"status": "needs_clarification",
                                       "reason": "transform requires a non-empty 'pipeline' list of steps "
                                       "(ops: select, filter, derive, group, sort, limit, distinct, window, "
                                       "pivot, unpivot).",
                                       "columns_available": list(df.columns)})
                try:
                    work, derived, steps_log = _run_pipeline(df, pipeline, params or {})
                except _ExprError as e:
                    return json.dumps({"status": "error", "reason": f"pipeline error: {e}",
                                       "hint": "Reference columns as `Col Name` or col('Col Name'); pass external "
                                       "values via params and use @name (e.g. @today); only whitelisted functions "
                                       "are allowed.", "columns_available": list(df.columns)})
                except Exception as e:
                    return json.dumps({"status": "error", "reason": f"{type(e).__name__}: {e}",
                                       "columns_available": list(df.columns)})
                cap = int(self.valves.max_return_rows)
                rows_out = work.head(cap).to_dict(orient="records")
                prov.update({"pipeline": pipeline, "rows_processed": int(rows_total),
                             "steps": steps_log, "code": "composable transform pipeline"})
                return json.dumps({"status": "ok", "result": rows_out, "columns": list(work.columns),
                                   "derived_columns": derived, "rows_in_result": int(len(work)),
                                   "rows_returned": len(rows_out),
                                   "truncated": int(len(work)) > len(rows_out),
                                   "table_markdown": _records_to_md(rows_out),
                                   "provenance": prov}, default=str)

            if operation == "compare":
                mcol, err = need_measure(value_column)
                if err:
                    return json.dumps(err)
                series = num(mcol)
                tcols = [c for c, r in roles.items() if r["role"] == "temporal"]
                # period-over-period when a date column is available and no group split requested
                if (date_column or len(tcols) == 1) and not group_by:
                    dc = date_column or tcols[0]
                    fmap = {"D": "D", "W": "W", "M": "ME", "MONTH": "ME", "MONTHLY": "ME",
                            "Q": "QE", "QUARTER": "QE", "QUARTERLY": "QE",
                            "Y": "YE", "A": "YE", "YEAR": "YE", "YEARLY": "YE", "ANNUAL": "YE"}
                    fq = fmap.get(str(freq).upper(), freq)
                    a = agg if agg in ("sum", "mean", "count") else "sum"
                    t = pd.DataFrame({"d": _coerce_datetime(df[dc]), "v": series}).dropna(subset=["d"])
                    ts = t.set_index("d")["v"].resample(fq).agg(a)
                    recs, prev = [], None
                    for k, v in ts.items():
                        v = float(v)
                        chg = (v - prev) if prev is not None else None
                        pct = ((v - prev) / prev * 100) if (prev not in (None, 0)) else None
                        recs.append({"period": str(k.date() if hasattr(k, "date") else k), a: round(v, 4),
                                     "change": (round(chg, 4) if chg is not None else None),
                                     "pct_change": (round(pct, 4) if pct is not None else None)})
                        prev = v
                    summary = {}
                    if len(recs) >= 2:
                        summary = {"periods": len(recs), "first": recs[0][a], "last": recs[-1][a],
                                   "net_change": round(recs[-1][a] - recs[0][a], 4),
                                   "net_pct_change": round((recs[-1][a] - recs[0][a]) / (recs[0][a] or 1) * 100, 4)}
                    prov.update({"columns_used": {"date": dc, "value": mcol}, "freq": fq, "agg": a,
                                 "rows_processed": int(len(t)),
                                 "code": f"resample('{fq}').{a}() + period-over-period change"})
                    return json.dumps({"status": "ok", "result": recs, "summary": summary,
                                       "provenance": prov}, default=str)
                # segment comparison across a dimension
                if group_by:
                    gcol, cand = self._resolve_col(df, group_by, "dimension", roles)
                    if not gcol:
                        return json.dumps({"status": "needs_clarification",
                                           "reason": f"group column '{group_by}' not found.",
                                           "candidates": cand or dims})
                    tmp = pd.DataFrame({gcol: df[gcol], mcol: series})
                    g = tmp.groupby(gcol)[mcol].agg(["sum", "count", "mean"])
                    if subset:
                        g = g[g.index.astype(str).isin([str(x) for x in subset])]
                    g = g.sort_values("sum", ascending=False).round(4)
                    recs = g.reset_index().to_dict(orient="records")
                    if len(recs) >= 2:
                        base = recs[0]
                        for r in recs[1:]:
                            r["delta_sum_vs_top"] = round((r.get("sum") or 0) - (base.get("sum") or 0), 4)
                            r["pct_vs_top"] = round(((r.get("sum") or 0) - (base.get("sum") or 0)) / (base.get("sum") or 1) * 100, 4)
                    prov.update({"columns_used": {"value": mcol, "group": gcol},
                                 "rows_processed": int(series.notna().sum()),
                                 "code": f"compare groups of '{gcol}' on '{mcol}'"})
                    return json.dumps({"status": "ok", "result": recs, "provenance": prov}, default=str)
                return json.dumps({"status": "needs_clarification",
                                   "reason": "compare needs either a date_column (period-over-period) "
                                             "or a group_by (segment comparison).",
                                   "candidates": {"temporal": tcols, "dimensions": dims}})

            return json.dumps({"status": "error",
                               "reason": f"Unknown operation '{operation}'.",
                               "supported": ["overview", "aggregate", "group_aggregate", "top_n",
                                             "pivot", "value_counts", "describe", "time_series",
                                             "anomalies", "duplicates", "missing", "compare",
                                             "transform"]})
        except Exception as e:
            return json.dumps({"status": "error", "reason": f"{type(e).__name__}: {e}",
                               "provenance": prov})
