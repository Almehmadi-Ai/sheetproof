"""
title: SheetProof
author: Abdulrahman Almehmadi
author_url: https://github.com/Almehmadi-Ai/sheetproof
funding_url: https://github.com/Almehmadi-Ai/sheetproof
version: 1.2.0
license: MIT
required_open_webui_version: 0.5.0
requirements: pandas, numpy, openpyxl, xlrd
description: Deterministic Excel and CSV analysis for Open WebUI. Reads the uploaded
  spreadsheet (access-checked), infers its schema, and runs pandas operations - statistics,
  group-by, pivot tables, time series, anomaly detection, de-duplication, filtering, and a
  safe composable transform pipeline - returning computed results with provenance, so the
  model reports numbers instead of calculating them.
"""

import ast
import asyncio
import csv
import difflib
import inspect
import io
import json
import os
import re
import threading
import types
import unicodedata
import zipfile
from collections import OrderedDict
from typing import Optional

import numpy as np
import pandas as pd
from pandas.tseries.api import guess_datetime_format
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Accepted inputs
# --------------------------------------------------------------------------- #
SPREADSHEET_EXTS = (".xlsx", ".xls", ".xlsm", ".csv", ".tsv")
# The only aggregation names ever handed to pandas (which dispatches strings to methods).
_AGG_FUNCS = ("sum", "mean", "count", "min", "max", "median", "std", "nunique")
_SERIES_AGGS = {
    "sum": lambda s: s.sum(),
    "mean": lambda s: s.mean(),
    "count": lambda s: s.count(),
    "min": lambda s: s.min(),
    "max": lambda s: s.max(),
    "median": lambda s: s.median(),
    "std": lambda s: s.std(),
    "nunique": lambda s: s.nunique(),
}
# Resample frequencies accepted for time_series / compare (anything else is rejected).
_FREQ_ALIASES = {
    "D": "D",
    "DAY": "D",
    "DAILY": "D",
    "W": "W",
    "WEEK": "W",
    "WEEKLY": "W",
    "M": "ME",
    "ME": "ME",
    "MONTH": "ME",
    "MONTHLY": "ME",
    "Q": "QE",
    "QE": "QE",
    "QUARTER": "QE",
    "QUARTERLY": "QE",
    "Y": "YE",
    "YE": "YE",
    "A": "YE",
    "YEAR": "YE",
    "YEARLY": "YE",
    "ANNUAL": "YE",
}
_FREQ_CHOICES = ["D", "W", "M", "Q", "Y"]
_ANOMALY_METHODS = ("iqr", "zscore", "pct_above_mean")
# Open WebUI file ids are str(uuid.uuid4()); anything else is rejected before any lookup.
_FILE_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Numbers stored as text
# A cell counts as a number only if the whole cell is one: an optional sign or (parentheses)
# negative, an optional currency symbol or ISO code, digits with one consistent thousands
# separator, an optional decimal part or exponent, and an optional percent sign (the shown value
# is kept: "12%" -> 12). "P0", "SKU-100" or "12-34" are not numbers. Arabic-Indic digits and the
# Arabic decimal / thousands separators are accepted.
# Whether "." or "," is the decimal separator is decided per column from values that can only be
# read one way ("1.234,56", "1,5", "1,234.56", "0.25"); values that read both ways ("1,234",
# "1.234") follow that decision, else a file-level / configured fallback.
# --------------------------------------------------------------------------- #
_DIGITS = str.maketrans(  # Arabic-Indic / Persian digits, Unicode spaces and minus
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹\u00a0\u202f\u2009\u2007\u2212",
    "01234567890123456789    -",
)
_CURRENCY = (
    r"(?:[$€£¥₹₩₺₽₪₫฿₦﷼]|ر\.?\s?س\.?|د\.?\s?إ\.?|(?:USD|EUR|GBP|JPY|CNY|INR|SAR|AED|QAR|KWD|BHD|"
    r"OMR|EGP|JOD|CHF|CAD|AUD|NZD|SEK|NOK|DKK|PLN|CZK|HUF|TRY|RUB|BRL|MXN|ZAR|SGD|HKD|KRW|IDR|"
    r"MYR|THB|PKR|ILS)(?![A-Za-z]))"
)
_NUMBER_CELL = re.compile(
    rf"(?P<s1>[+-])?\s*(?:{_CURRENCY}\s*)?(?P<s2>[+-])?\s*"
    r"(?P<core>\d[\d.,'’٫٬ ]*|[.,٫]\d+)(?P<exp>[eE][+-]?\d+)?"
    rf"\s*(?:[%٪]\s*)?(?:{_CURRENCY})?\s*(?:[%٪])?"
)
_DOT_FORMS = re.compile(r"\d+(?:\.\d*)?|\.\d+|[1-9]\d{0,2}(?:,\d{3})+(?:\.\d*)?|[1-9]\d?(?:,\d{2})+,\d{3}(?:\.\d*)?")
_COMMA_FORMS = re.compile(r"\d+(?:,\d*)?|,\d+|[1-9]\d{0,2}(?:\.\d{3})+(?:,\d*)?")
_AMBIGUOUS_DOT = re.compile(r"\s*[+-]?[1-9]\d{0,2}\.\d{3}\s*")  # "1.234": 1.234 or 1234
_DECIMAL_SEPARATORS = (".", ",")


def _is_number(v):
    return isinstance(v, (int, float, np.integer, np.floating)) and not isinstance(v, (bool, np.bool_))


def _parse_number_text(text):
    """Read one text cell. Returns (value if '.' is the decimal separator, value if ',' is);
    an entry is None when the cell is not a number under that convention."""
    t = text.strip() if text.isascii() else text.translate(_DIGITS).strip()
    neg = len(t) > 2 and t[0] == "(" and t[-1] == ")"
    if neg:
        t = t[1:-1].strip()
    m = _NUMBER_CELL.fullmatch(t)
    if not m or (m["s1"] and m["s2"]):
        return None, None
    sign = -1.0 if (neg or "-" in ((m["s1"] or "") + (m["s2"] or ""))) else 1.0
    core = m["core"].strip()
    if m["exp"]:  # scientific notation is only written with a '.' decimal point
        if not re.fullmatch(r"\d+(?:\.\d*)?|\.\d+", core):
            return None, None
        v = sign * float(core + m["exp"])
        if not np.isfinite(v):
            return None, None
        return (v, None) if "." in core else (v, v)
    thousands = set() if core.isascii() and "'" not in core and " " not in core else set(core) & set("'’ ٬")
    if thousands or "٫" in core:  # apostrophe / space / Arabic separators are unambiguous
        if len(thousands) > 1:
            return None, None
        tsep = re.escape(thousands.pop()) if thousands else None
        int_re = rf"[1-9]\d{{0,2}}(?:{tsep}\d{{3}})+" if tsep else r"\d*"
        mm = re.fullmatch(rf"({int_re})(?:([.,٫])(\d+))?", core)
        if not mm or not mm[1] + (mm[3] or ""):
            return None, None
        v = sign * float(re.sub(r"\D", "", mm[1]) + "." + (mm[3] or "0"))
        return (v, v) if mm[2] in (None, "٫") else ((v, None) if mm[2] == "." else (None, v))
    dot = sign * float(core.replace(",", "")) if _DOT_FORMS.fullmatch(core) else None
    comma = sign * float(core.replace(".", "").replace(",", ".")) if _COMMA_FORMS.fullmatch(core) else None
    return dot, comma


def _parse_numbers(s: pd.Series, decimal=None):
    """Parse a column into floats. Returns (values, info): info reports the decimal
    separator used and why, how many distinct values were ambiguous, and how many non-blank
    cells were not numbers. `decimal` is the fallback for a column whose values never
    show which separator is the decimal one ('.' when None)."""
    if pd.api.types.is_numeric_dtype(s):
        return pd.to_numeric(s, errors="coerce"), {}
    nonnull = s[s.notna()]
    uniq = pd.unique(nonnull.to_numpy(dtype=object))
    # Fast path: plain numbers such as "1234.5" or "-12" parse in C; of those only the
    # "1.234" shape can also be read the other way. Everything else goes through the parser.
    texts = [v for v in uniq if isinstance(v, str)]
    plain = dict(zip(texts, pd.to_numeric(pd.Series(texts, dtype=object), errors="coerce").tolist(), strict=True))
    readings, votes, ambiguous = {}, {".": 0, ",": 0}, 0
    for v in uniq:
        if isinstance(v, str):
            f = plain[v]
            if f == f and abs(f) != float("inf") and "," in v:
                f = float("nan")
            if f != f or abs(f) == float("inf"):
                r = _parse_number_text(v)
            elif "." not in v:
                r = (float(f), float(f))
            elif _AMBIGUOUS_DOT.fullmatch(v):
                r = (float(f), float(v.replace(".", "")))
            else:
                r = (float(f), None)
        elif _is_number(v) and np.isfinite(float(v)):
            r = (float(v), float(v))
        else:
            r = (None, None)
        readings[v] = r
        if r[0] is not None and r[1] is None:
            votes["."] += 1
        elif r[1] is not None and r[0] is None:
            votes[","] += 1
        elif r[0] is not None and r[0] != r[1]:
            ambiguous += 1
    if votes["."] != votes[","]:
        dec, basis = max(votes, key=votes.get), "column values"
    else:
        dec, basis = (decimal if decimal in _DECIMAL_SEPARATORS else "."), "fallback"
    lookup = {
        k: (r[0] if r[1] is None else r[1] if r[0] is None else r[_DECIMAL_SEPARATORS.index(dec)])
        for k, r in readings.items()
    }
    values = np.full(len(s), np.nan)
    if len(nonnull):  # positional, so duplicate index labels cannot misalign values
        values[s.notna().to_numpy()] = pd.to_numeric(nonnull.map(lookup), errors="coerce").to_numpy(dtype=float)
    out = pd.Series(values, index=s.index)
    info = {}
    if votes["."] or votes[","] or ambiguous:
        info = {"decimal_separator": dec, "decided_by": basis, "ambiguous_values": ambiguous}
    unparsed = int(len(nonnull) - out.notna().sum())
    if unparsed:
        info["non_numeric_cells"] = unparsed
    return out, info


def _decimal_evidence(df: pd.DataFrame, sample: int = 1000) -> Optional[str]:
    """File-level decimal separator: majority of the unambiguous values in the first
    `sample` distinct values of each text column, or None when nothing is unambiguous."""
    votes = {".": 0, ",": 0}
    for col in df.columns:
        s = df[col]
        if pd.api.types.is_numeric_dtype(s) or pd.api.types.is_bool_dtype(s):
            continue
        for v in pd.unique(s.dropna().to_numpy(dtype=object))[:sample]:
            if isinstance(v, str):
                dot, comma = _parse_number_text(v)
                if dot is not None and comma is None:
                    votes["."] += 1
                elif comma is not None and dot is None:
                    votes[","] += 1
    if votes["."] == votes[","]:
        return None
    return max(votes, key=votes.get)


def _coerce_numeric(s: pd.Series, decimal=None) -> pd.Series:
    """Locale-tolerant, strict numeric coercion (see _parse_numbers)."""
    return _parse_numbers(s, decimal)[0]


# --------------------------------------------------------------------------- #
# Column roles
# --------------------------------------------------------------------------- #
def _coerce_datetime(s: pd.Series) -> pd.Series:
    """pd.to_datetime with pandas' own format inference done up front: the format guessed from
    the first value, or per-element parsing ("mixed") when none can be guessed, which is what
    pandas falls back to anyway. Passing it explicitly avoids the "Could not infer format"
    warning without warnings.catch_warnings(), which is not thread-safe."""
    if pd.api.types.is_datetime64_any_dtype(s):
        return s
    fmt, values = None, s.dropna()
    if len(values) and isinstance(values.iloc[0], str):
        fmt = guess_datetime_format(values.iloc[0]) or "mixed"
    return pd.to_datetime(s, errors="coerce", format=fmt)


def _is_numeric_col(s: pd.Series, thresh: float = 0.8, decimal=None) -> bool:
    n = _coerce_numeric(s, decimal)
    nonnull = s.notna().sum()
    return nonnull > 0 and (n.notna().sum() / max(nonnull, 1)) >= thresh


def _is_datetime_col(s: pd.Series, thresh: float = 0.8) -> bool:
    # Every other dtype is tried, not only object: pandas 3 stores text as 'str'.
    if pd.api.types.is_numeric_dtype(s) or pd.api.types.is_bool_dtype(s):
        return False
    d = _coerce_datetime(s)
    nonnull = int(s.notna().sum())
    return nonnull > 0 and (int(d.notna().sum()) / max(nonnull, 1)) >= thresh


def _classify(df: pd.DataFrame, decimal=None) -> dict:
    """Role, dtype, counts and sample values of every column, inferred from the values alone."""
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
        elif _is_numeric_col(s, decimal=decimal):
            # numeric -> measure. Only an integer-valued, near-unique column is an id
            # (continuous measures are also near-unique, so cardinality alone is wrong).
            ncoerced = _coerce_numeric(s, decimal).dropna()
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


def _detect_currency(df: pd.DataFrame) -> Optional[str]:
    """The currency the first 50 rows mention, "MIXED:<codes>" for several, or None."""
    syms = {
        "$": "USD",
        "€": "EUR",
        "£": "GBP",
        "¥": "JPY",
        "SAR": "SAR",
        "AED": "AED",
        "USD": "USD",
        "EUR": "EUR",
        "GBP": "GBP",
    }
    sample = df.head(50).astype(str).values.ravel().tolist()
    found = set()
    for v in map(str, sample):  # astype(str) leaves NaN as a float on pandas 3
        for k, code in syms.items():
            if k in v:
                found.add(code)
    if len(found) == 1:
        return found.pop()
    if len(found) > 1:
        return "MIXED:" + ",".join(sorted(found))
    return None


# --------------------------------------------------------------------------- #
# Reading spreadsheets
# Size limits are checked before (file size, ZIP directory) and while (row budget) a file is
# parsed. For CSV/TSV the delimiter and header row are detected on a text sample, then pandas' C
# engine reads the table.
# --------------------------------------------------------------------------- #
class _LoadError(Exception):
    """A file refused before or while loading (a size limit); the message is user-facing."""


_MAX_ZIP_RATIO = 100  # an .xlsx member expanding more than 100:1 is treated as a zip bomb ...
_ZIP_RATIO_MIN_BYTES = 10 * 2**20  # ... once it expands beyond 10 MB
_CSV_DELIMITERS = ("\t", ";", "|", ",")  # on a tie the earlier one wins: ',' is common inside values
_CSV_SAMPLE_CHARS = 256 * 1024
_HEADER_SCAN_ROWS = 50
_TRUE_FALSE = {"True": True, "TRUE": True, "true": True, "False": False, "FALSE": False, "false": False}


def _detect_header(raw: pd.DataFrame) -> int:
    """Index of the header row: the first of the first 30 rows with at least 60% as many cells
    as the fullest row (and at least two), so title, banner and blank rows are skipped."""
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


def _csv_records(text, delimiter, complete):
    """The first records of a text sample. Like pandas' row counting for skiprows, a blank
    line is a (empty) record and a quoted field may span several lines."""
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    records = []
    while len(records) < _HEADER_SCAN_ROWS:
        try:
            records.append(next(reader))
        except StopIteration:
            if not complete and records:  # the sample may end mid-record
                records.pop()
            break
        except csv.Error:
            break
    return records


def _sniff_csv(text, complete, delimiter=None):
    """Pick the delimiter that splits the most rows into the same number (>= 2) of fields,
    then locate the header row past any title / banner / blank rows. Returns
    (delimiter, number of rows to skip before the header, number of columns)."""
    best = None
    for d in [delimiter] if delimiter else _CSV_DELIMITERS:
        records = _csv_records(text, d, complete)
        widths = [len(r) for r in records if len(r) >= 2]
        score = (0, 0)
        if widths:
            mode = max(set(widths), key=lambda w: (widths.count(w), w))
            score = (widths.count(mode), mode)
        if best is None or score > best[0]:
            best = (score, d, records)
    _, d, records = best
    raw = pd.DataFrame([[v if v.strip() else None for v in r] for r in records])
    header = _detect_header(raw) if records else 0
    width = max((len(r) for r in records[header:]), default=1)
    return d, header, width


def _type_csv_columns(df, decimal=None):
    """read_csv(dtype=str) keeps every cell as text. Restore the dtypes pandas would infer
    (numbers, True/False), except for a column of '1.234'-style values in a file whose
    numbers use ',' as the decimal separator: it stays text, so it is read as 1234."""
    fallback = decimal if decimal in _DECIMAL_SEPARATORS else _decimal_evidence(df)
    for col in df.columns:
        s = df[col]
        n_values = int(s.notna().sum())
        if not n_values:
            continue
        numbers = pd.to_numeric(s, errors="coerce")
        if int(numbers.notna().sum()) == n_values:
            if fallback == "," and _parse_numbers(s, ",")[1].get("decimal_separator") == ",":
                continue
            df[col] = numbers
        elif s.dropna().isin(list(_TRUE_FALSE)).all():
            df[col] = s.map(_TRUE_FALSE)
    return df


def _check_zip(path, max_uncompressed_bytes):
    """Inspect an .xlsx/.xlsm central directory before any parser touches it. The declared
    sizes are binding: Python's zipfile never inflates a member beyond its declared size."""
    try:
        with zipfile.ZipFile(path) as zf:
            members = zf.infolist()
    except (zipfile.BadZipFile, OSError):
        raise _LoadError("The file is not a valid Excel workbook (.xlsx/.xlsm are ZIP archives).") from None
    total = 0
    for info in members:
        total += info.file_size
        ratio = info.file_size / max(info.compress_size, 1)
        if info.file_size > _ZIP_RATIO_MIN_BYTES and ratio > _MAX_ZIP_RATIO:
            raise _LoadError(
                f"Workbook part '{info.filename}' expands {ratio:,.0f}:1, which looks like a zip "
                "bomb; the file was not opened."
            )
    if total > max_uncompressed_bytes:
        raise _LoadError(
            f"Workbook contents expand to {total / 2**20:,.0f} MB, above the "
            f"{max_uncompressed_bytes / 2**20:,.0f} MB limit (max_uncompressed_mb valve)."
        )


def _declared_width(xl, sheet):
    """Column count a sheet declares (openpyxl read-only dimension / xlrd ncols), if any. Must be
    called before the sheet is parsed: a read-only worksheet cannot report it afterwards."""
    try:
        book = xl.book
        if hasattr(book, "sheet_by_name"):  # xlrd (.xls)
            return int(book.sheet_by_name(sheet).ncols)
        width = book[sheet].max_column
        return int(width) if width else None
    except Exception:
        return None


def _row_budget(max_cells, width):
    """Most data rows a table of `width` columns may have under max_cells."""
    return max(int(max_cells) // max(int(width or 1), 1), 1)


def _too_large(rows, width, max_cells):
    return _LoadError(
        f"File too large: more than {rows:,} rows x {width} columns exceeds max_cells="
        f"{int(max_cells):,}. Increase the max_cells valve to process it."
    )


def _decimal_setting(valves):
    setting = str(valves.decimal_separator or "auto").strip().lower()
    return setting if setting in _DECIMAL_SEPARATORS else None


def _decimal_fallback(df, valves):
    """Decimal separator for columns whose own values are ambiguous ('1,234')."""
    setting = _decimal_setting(valves)
    if setting:
        return setting, "decimal_separator valve"
    evidence = _decimal_evidence(df)
    if evidence:
        return evidence, "other values in the file"
    return ".", "default ('.'); set the decimal_separator valve if '1,234' means 1.234"


def _read(path, valves, sheet=None):
    """Parse a spreadsheet within the configured limits (checked before and while reading)."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in SPREADSHEET_EXTS:
        raise _LoadError(f"Unsupported file type '{ext}'.")
    max_cells = valves.max_cells
    size = os.path.getsize(path)
    if size > float(valves.max_file_mb) * 2**20:
        raise _LoadError(
            f"File is {size / 2**20:,.1f} MB, above the {float(valves.max_file_mb):g} MB limit (max_file_mb valve)."
        )
    if ext in (".csv", ".tsv"):
        with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
            text = f.read(_CSV_SAMPLE_CHARS + 1)
        sep, header_row, width = _sniff_csv(
            text[:_CSV_SAMPLE_CHARS], len(text) <= _CSV_SAMPLE_CHARS, "\t" if ext == ".tsv" else None
        )
        budget = _row_budget(max_cells, width)
        df = pd.read_csv(
            path, sep=sep, skiprows=header_row, header=0, dtype=str, encoding="utf-8-sig", nrows=budget + 1
        )
        if len(df) > budget:
            raise _too_large(budget, width, max_cells)
        return _type_csv_columns(df, _decimal_setting(valves)), ["(csv)"], "(csv)"
    if ext in (".xlsx", ".xlsm"):
        _check_zip(path, float(valves.max_uncompressed_mb) * 2**20)
    with pd.ExcelFile(path) as xl:
        sheets = xl.sheet_names
        declared = {sh: _declared_width(xl, sh) for sh in sheets}  # read before any sheet is parsed
        chosen = sheet if (sheet in sheets) else None
        if chosen is None:
            # the sheet with the most cells is the data, not a title or notes tab
            best, best_cells = sheets[0], -1
            for sh in sheets:
                d = xl.parse(sh, header=None, nrows=200)
                cells = int(d.notna().sum().sum())
                if cells > best_cells:
                    best, best_cells = sh, cells
            chosen = best
        raw = xl.parse(chosen, header=None, nrows=_HEADER_SCAN_ROWS)
        hdr = _detect_header(raw)
        width = max(raw.shape[1], declared.get(chosen) or 0, 1)
        budget = _row_budget(max_cells, width)
        df = xl.parse(chosen, header=hdr, nrows=budget + 1)
    if len(df) > budget:
        raise _too_large(budget, width, max_cells)
    return df, sheets, chosen


# --------------------------------------------------------------------------- #
# File access and the parsed-file cache
# Every file id reaches the tool from the client request, so each one must pass _can_read
# before its path is used or the cache is consulted.
# --------------------------------------------------------------------------- #
async def _maybe_await(value):
    """Open WebUI's model layer is async since 0.7 and synchronous before it."""
    return await value if inspect.isawaitable(value) else value


async def _can_read(rec, user):
    """Open WebUI's own read rule for a file (routers/files.py in 0.12.2): the owner, an
    admin, or a user its access control grants read access (a knowledge base, channel,
    shared chat, note or model the file is attached to). Never an unscoped lookup."""
    uid = user.get("id")
    if not uid:
        return False
    if getattr(rec, "user_id", None) == uid or user.get("role") == "admin":
        return True
    try:
        try:
            from open_webui.utils.access_control.files import has_access_to_file
        except ImportError:  # Open WebUI < 0.7 kept the helper in the files router
            from open_webui.routers.files import has_access_to_file
        try:
            from open_webui.models.users import UserModel

            principal = UserModel(**user)
        except Exception:
            principal = types.SimpleNamespace(**user)
        return bool(await _maybe_await(has_access_to_file(rec.id, "read", principal)))
    except Exception:
        return False  # fail closed


async def _local_path(rec):
    """Local path of an authorized file record (fetched from S3/GCS/Azure when used)."""
    path = getattr(rec, "path", None)
    if not path:
        return None

    def fetch():  # storage download and filesystem checks block, so they run in a thread
        local = path
        try:
            from open_webui.storage.provider import Storage

            local = Storage.get_file(path)
        except ImportError:
            pass
        return local if local and os.path.isfile(local) else None

    try:
        return await asyncio.to_thread(fetch)
    except Exception:
        return None


async def _resolve_path(file_id, files, metadata, user):
    """Return (local path, file id, error).

    Candidate ids come from the explicit file_id, the files attached to the message, or
    the router's stash. All three originate in the client request, so every id is
    format-checked and must pass _can_read; there is no unscoped or filesystem fallback.
    An explicit file_id is authoritative: it is never swapped for a different file."""
    user = user or {}
    if not user.get("id"):
        return None, None, "No signed-in user; uploaded files cannot be accessed."
    try:
        from open_webui.models.files import Files
    except ImportError:
        return None, None, "Open WebUI's file store is not available."
    explicit = file_id is not None and str(file_id).strip().lower() not in ("", "none", "null")
    if explicit:
        candidates = [str(file_id).strip()]
        if not _FILE_ID_RE.fullmatch(candidates[0]):
            return (
                None,
                None,
                (f"Invalid file_id '{candidates[0][:80]}'. Omit file_id to use the spreadsheet attached to this chat."),
            )
    else:
        candidates = []
        for f in (files or []) + (((metadata or {}).get("files")) or []):
            if not isinstance(f, dict):
                continue
            fid = f.get("id") or (f.get("file") or {}).get("id")
            name = f.get("name") or (f.get("file") or {}).get("filename") or ""
            if fid and (not name or str(name).lower().endswith(SPREADSHEET_EXTS)):
                candidates.append(fid)
        stash = (metadata or {}).get("analyst_file_ids")  # set by sheetproof_router
        candidates += list(stash) if isinstance(stash, (list, tuple)) else []
        candidates = [c for c in dict.fromkeys(c for c in candidates if isinstance(c, str)) if _FILE_ID_RE.fullmatch(c)]
    for fid in candidates:
        try:
            rec = await _maybe_await(Files.get_file_by_id(fid))
        except Exception:
            rec = None
        if rec is None or not await _can_read(rec, user):
            continue
        path = await _local_path(rec)
        if path and os.path.splitext(path)[1].lower() in SPREADSHEET_EXTS:
            return path, fid, None
        if explicit:
            return None, None, "That file is not an uploaded spreadsheet (.xlsx, .xls, .xlsm, .csv, .tsv)."
    if explicit:
        return None, None, "File not found, or you do not have access to it."
    return None, None, "No uploaded spreadsheet found for this message."


class _FrameCache:
    """Bounded, thread-safe LRU of parsed spreadsheets, shared by every user of a Tools instance.

    It is only consulted after a caller passed authorization for the file, and its keys
    contain that authorized file id, the file's identity on disk (real path, mtime, size)
    and every option that affects parsing, so an entry can neither leak to a caller who may
    not read the file nor go stale when the file changes. Entries are treated as read-only."""

    def __init__(self):
        self._entries = OrderedDict()
        self._lock = threading.Lock()
        self._bytes = 0

    def get(self, key):
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
            return entry

    def put(self, key, entry, max_entries, max_bytes):
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._bytes -= old["bytes"]
            if max_entries > 0 and entry["bytes"] <= max_bytes:
                self._entries[key] = entry
                self._bytes += entry["bytes"]
            self._evict(max_entries, max_bytes)

    def charge(self, key, nbytes, max_entries, max_bytes):
        """Account for memory added to a cached entry (memoized parsed columns)."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                entry["bytes"] += nbytes
                self._bytes += nbytes
            self._evict(max_entries, max_bytes)

    def _evict(self, max_entries, max_bytes):
        while self._entries and (len(self._entries) > max_entries or self._bytes > max_bytes):
            _, evicted = self._entries.popitem(last=False)
            self._bytes -= evicted["bytes"]

    def __len__(self):
        with self._lock:
            return len(self._entries)


def _cache_limits(valves):
    return int(valves.cache_max_entries), float(valves.cache_max_mb) * 2**20


def _load_entry(cache, valves, fid, path, sheet):
    """Parsed file and schema for an authorized file id, from the cache when unchanged."""
    st = os.stat(path)
    key = (
        fid,
        os.path.realpath(path),
        st.st_mtime_ns,
        st.st_size,
        sheet,
        _decimal_setting(valves),
        int(valves.max_cells),
        float(valves.max_file_mb),
        float(valves.max_uncompressed_mb),
    )
    entry = cache.get(key)
    if entry is not None:
        return entry, key
    df, sheets, chosen = _read(path, valves, sheet)
    if df.shape[0] * max(df.shape[1], 1) > valves.max_cells:  # backstop; _read already bounds rows
        raise _too_large(df.shape[0], df.shape[1], valves.max_cells)
    # column names become stripped strings; fully empty columns and rows are dropped
    df.columns = [str(c).strip() for c in df.columns]
    df = df.dropna(axis=1, how="all").dropna(how="all").reset_index(drop=True)
    decimal, decided_by = _decimal_fallback(df, valves)
    entry = {
        "df": df,
        "sheets": sheets,
        "sheet": chosen,
        "decimal": decimal,
        "decimal_decided_by": decided_by,
        "roles": _classify(df, decimal),
        "numbers": {},
        "bytes": int(df.memory_usage(index=True, deep=True).sum()),
    }
    cache.put(key, entry, *_cache_limits(valves))
    return entry, key


async def _load(cache, valves, file_id, files, metadata, user, sheet=None):
    # authorization first: the cache below is only ever reached for an authorized file id
    path, fid, err = await _resolve_path(file_id, files, metadata, user)
    if not path:
        return None, {"error": err}
    try:  # parse off the event loop so one large file does not stall other users' requests
        entry, key = await asyncio.to_thread(_load_entry, cache, valves, fid, path, sheet)
    except _LoadError as e:
        return None, {"error": str(e)}
    except Exception as e:
        detail = str(e).replace(path, os.path.basename(path))
        return None, {"error": f"Could not read the spreadsheet: {type(e).__name__}: {detail}"}
    return entry["df"], {
        "path": path,
        "file_id": fid,
        "sheets": entry["sheets"],
        "sheet": entry["sheet"],
        "decimal": entry["decimal"],
        "decimal_decided_by": entry["decimal_decided_by"],
        "roles": entry["roles"],
        "numbers": entry["numbers"],
        "cache_key": key,
    }


# --------------------------------------------------------------------------- #
# Column-name matching
# Names are NFKC-normalized and case-folded, Arabic diacritics and tatweel are dropped, alef /
# yaa / taa-marbuta variants are unified, and only letters and digits of any script are kept. A
# fuzzy match must be clearly the best candidate; otherwise the caller asks which column was meant.
# --------------------------------------------------------------------------- #
_AR_MARKS = re.compile("[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed\u0640]")  # harakat, Quranic marks, tatweel
_AR_FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ی": "ي", "ة": "ه", "ک": "ك"})
_MIN_NAME_SCORE = 0.8


def _fold_name(name):
    s = unicodedata.normalize("NFKC", str(name)).casefold()
    return _AR_MARKS.sub("", s).translate(_AR_FOLD)


def _norm_name(name):
    """Matching key: 'Order Date', 'order_date' and 'ORDER-DATE' all become 'orderdate'."""
    return "".join(str(unicodedata.digit(ch, ch)) if ch.isdigit() else ch for ch in _fold_name(name) if ch.isalnum())


def _name_tokens(name):
    """Words of a name for fuzzy scoring; a leading Arabic article (ال) is not a word."""
    words = re.findall(r"[^\W_]+", _fold_name(name))
    return [w[2:] if w.startswith("ال") and len(w) > 3 else w for w in words]


def _name_score(a, b):
    """Similarity in [0, 1] of a requested name `a` to a column name `b` (typos, word order,
    contained names). Every number in `a` must also appear in `b`."""
    ta, tb = _name_tokens(a), _name_tokens(b)
    ka, kb = _norm_name("".join(ta)), _norm_name("".join(tb))
    if not ka or not kb:
        return 0.0
    digits_b = re.findall(r"\d+", kb)
    if any(d not in digits_b for d in re.findall(r"\d+", ka)):
        return 0.0  # "Revenue 2026" is not "Revenue 2024", "Q2" is not "Q1"
    ratio = difflib.SequenceMatcher(None, ka, kb, autojunk=False).ratio()
    short, long_ = sorted((ka, kb), key=len)
    contained = 0.8 if len(short) >= 3 and short in long_ else 0.0
    sa, sb = set(ta), set(tb)
    jaccard = len(sa & sb) / len(sa | sb) if sa and sb else 0.0
    return max(ratio, contained, jaccard)


def _resolve_simple(df, name):
    """The column called `name`, else the ONE column with the same normalized name. None when
    there is no such column or several (the expression grammar never fuzzy-matches)."""
    try:
        if name in df.columns:
            return name
    except TypeError:
        return None
    key = _norm_name(name)
    hits = [c for c in df.columns if key and _norm_name(c) == key]
    return hits[0] if len(hits) == 1 else None


def _resolve_col(df, name, min_gap):
    """Map a user/model-supplied name to a real column. Returns (col, candidates).

    Exact name, then the single column with the same normalized name, then a fuzzy match
    that must score at least _MIN_NAME_SCORE and beat the runner-up by `min_gap` (the
    ambiguity_min_gap valve). Otherwise (None, closest candidates): the caller asks."""
    if name is None or not str(name).strip():
        return None, []
    cols = list(df.columns)
    if name in cols:
        return name, []
    key = _norm_name(name)
    exact = [c for c in cols if key and _norm_name(c) == key]
    if len(exact) == 1:
        return exact[0], []
    if len(exact) > 1:
        return None, exact  # e.g. "Sales" and "sales": never pick one
    scored = sorted(((_name_score(name, c), c) for c in cols), key=lambda t: -t[0])
    candidates = [c for s, c in scored if s >= 0.5][:5]
    if scored and scored[0][0] >= _MIN_NAME_SCORE:
        runner_up = scored[1][0] if len(scored) > 1 else 0.0
        if scored[0][0] - runner_up >= float(min_gap):
            return scored[0][1], candidates
    return None, candidates


# --------------------------------------------------------------------------- #
# Expression language
# `filters` and the filter / derive steps of `transform` are model-supplied text. They are parsed
# with ast and checked against an allow-list (column references, params, literals, arithmetic,
# comparisons, boolean logic, the functions in _FUNCS and the column methods in _METHODS /
# _STR_METHODS) before anything is evaluated; they never reach eval, exec or DataFrame.query.
# --------------------------------------------------------------------------- #
class _ExprError(Exception):
    pass


# Budgets that keep a single expression cheap to parse and evaluate.
_MAX_EXPR_CHARS = 2000
_MAX_EXPR_NODES = 400
_MAX_EXPR_DEPTH = 64
_MAX_POW_EXPONENT = 100  # ** needs a constant exponent and is evaluated in floating point
_MAX_TEXT_LEN = 10_000  # longest text a concatenation may produce in one cell
_ALLOWED_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.BinOp,
    ast.UnaryOp,
    ast.Compare,
    ast.Call,
    ast.Attribute,
    ast.keyword,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.And,
    ast.Or,
    ast.Not,
    ast.Invert,
    ast.USub,
    ast.UAdd,
    ast.BitAnd,
    ast.BitOr,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
)
_CONST_TYPES = (str, int, float, bool, type(None))
# pandas-style column methods, dispatched explicitly by _SafeExpr._method (never getattr).
_METHODS = {"isna", "notna", "isnull", "notnull", "between", "isin"}
_STR_METHODS = {"contains", "startswith", "endswith", "lower", "upper", "strip", "len"}
_METHOD_KWARGS = {"between": {"inclusive"}, "contains": {"case", "na", "regex"}}
_EXPR_HINT = (
    "Reference columns as `Col Name` (backticks) or col('Col Name'); pass external values via params "
    "and use @name (e.g. @today); combine conditions with and / or / not (or & | ~). Allowed: "
    "comparisons (== != < <= > >=), in [...] / not in [...], arithmetic, the whitelisted functions, "
    "and the column methods .isna() .notna() .between(a, b) .isin([...]) .str.contains('x') "
    ".str.startswith('x') .str.endswith('x') .str.lower() .str.upper() .str.strip() .str.len()."
)


def _rewrite_refs(expr):
    """Turn `quoted column names` into col('...') and @name into param('name').

    Quote-aware: text inside '...' / "..." string literals is left untouched, so values
    such as 'a@b.com' or 'it`s' are not rewritten."""
    out, i, n, quote = [], 0, len(expr), None
    while i < n:
        ch = expr[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(expr[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
        elif ch in "'\"":
            quote = ch
            out.append(ch)
            i += 1
        elif ch == "`":
            j = expr.find("`", i + 1)
            if j < 0:
                raise _ExprError("unterminated `column name` (missing closing backtick)")
            out.append("col(" + repr(expr[i + 1 : j]) + ")")
            i = j + 1
        elif ch == "@":
            m = re.match(r"@([^\W\d]\w*)", expr[i:])
            if not m:
                raise _ExprError("'@' must be followed by a parameter name, e.g. @today")
            out.append("param(" + repr(m.group(1)) + ")")
            i += m.end()
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _is_method_call(func):
    """True for `<expr>.isna()`-style and `<expr>.str.contains()`-style method references."""
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr in _METHODS:
        return True
    return func.attr in _STR_METHODS and isinstance(func.value, ast.Attribute) and func.value.attr == "str"


def _parse_expr(expr):
    """Parse an expression and validate it against the allow-list before any evaluation."""
    if not isinstance(expr, str):
        raise _ExprError("expression must be a string")
    if len(expr) > _MAX_EXPR_CHARS:
        raise _ExprError(f"expression is too long (max {_MAX_EXPR_CHARS} characters)")
    src = _rewrite_refs(expr).strip()
    try:
        tree = ast.parse(src, mode="eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError) as ex:
        raise _ExprError(f"cannot parse '{expr}': {getattr(ex, 'msg', None) or ex}") from None
    allowed_attrs, count, stack = set(), 0, [(tree, 0)]
    while stack:  # parents are always visited before their children
        node, depth = stack.pop()
        count += 1
        if count > _MAX_EXPR_NODES:
            raise _ExprError("expression is too complex")
        if depth > _MAX_EXPR_DEPTH:
            raise _ExprError("expression is nested too deeply")
        if not isinstance(node, _ALLOWED_NODES):
            raise _ExprError(f"disallowed syntax: {type(node).__name__}")
        if isinstance(node, ast.Constant) and not isinstance(node.value, _CONST_TYPES):
            raise _ExprError(f"unsupported literal: {node.value!r}")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise _ExprError("invalid name")
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _FUNCS:
                if node.keywords:
                    raise _ExprError(f"{func.id}() does not take keyword arguments")
            elif _is_method_call(func):
                allowed_attrs.add(id(func))
                if func.attr in _STR_METHODS:
                    allowed_attrs.add(id(func.value))
                ok_kw = _METHOD_KWARGS.get(func.attr, set())
                for kw in node.keywords:
                    if kw.arg is None:
                        raise _ExprError("**keyword unpacking is not allowed")
                    if kw.arg not in ok_kw:
                        raise _ExprError(f".{func.attr}() does not accept the argument '{kw.arg}'")
            else:
                raise _ExprError("only whitelisted functions and column methods may be called")
        if isinstance(node, ast.Attribute) and id(node) not in allowed_attrs:
            raise _ExprError("attribute access is not allowed (only the listed column methods)")
        stack.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    return tree


# --------------------------------------------------------------------------- #
# Expression evaluation
# Comparisons are typed: number literals compare numerically, text literals compare as dates on a
# date column and as exact text otherwise.
# --------------------------------------------------------------------------- #
def _to_dt(x):
    return _coerce_datetime(x) if isinstance(x, pd.Series) else pd.to_datetime(x, errors="coerce")


def _to_bool(x):
    return x.fillna(False).astype(bool) if isinstance(x, pd.Series) else bool(x)


def _as_series(ev, x):
    return x if isinstance(x, pd.Series) else pd.Series([x] * len(ev.df), index=ev.df.index)


def _as_series_like(val, df):
    return val.reindex(df.index) if isinstance(val, pd.Series) else pd.Series([val] * len(df), index=df.index)


# Functions callable in expressions (_FUNCS). Each receives the evaluator first.
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
    if isinstance(n, bool) or not isinstance(n, (int, float)) or not -15 <= n <= 15:
        raise _ExprError("round() needs a number of decimals between -15 and 15")
    return _coerce_numeric(_as_series(ev, x), ev.decimal).round(int(n))


def _f_num(ev, x):
    return _coerce_numeric(_as_series(ev, x), ev.decimal)


def _f_param(ev, name):
    key = str(name)
    if key not in ev.params:
        raise _ExprError(
            f"unknown parameter @{key}; pass it via params, e.g. "
            f'params={{"{key}": "2026-06-16"}} (do not drop the value)'
        )
    val = ev.params[key]
    items = val if isinstance(val, (list, tuple)) else [val]
    if not all(isinstance(v, _CONST_TYPES) for v in items):
        raise _ExprError(f"parameter @{key} must be a number, text, true/false, null, or a list of those")
    return list(val) if isinstance(val, (list, tuple)) else val


_FUNCS = {
    "col": _f_col,
    "param": _f_param,
    "where": _f_where,
    "coalesce": _f_coalesce,
    "datediff": _f_datediff,
    "isnull": lambda ev, x: _as_series(ev, x).isna(),
    "isnan": lambda ev, x: _as_series(ev, x).isna(),
    "notnull": lambda ev, x: _as_series(ev, x).notna(),
    "abs": lambda ev, x: _as_series(ev, x).abs(),
    "round": _f_round,
    "num": _f_num,
    "number": _f_num,
    "text": lambda ev, x: _as_series(ev, x).astype(str),
    "str": lambda ev, x: _as_series(ev, x).astype(str),
    "lower": lambda ev, x: _as_series(ev, x).astype(str).str.lower(),
    "upper": lambda ev, x: _as_series(ev, x).astype(str).str.upper(),
    "len": lambda ev, x: _as_series(ev, x).astype(str).str.len(),
    "contains": lambda ev, s, sub: (
        _as_series(ev, s).astype(str).str.contains(str(sub), case=False, na=False, regex=False)
    ),
    "startswith": lambda ev, s, p: _as_series(ev, s).astype(str).str.startswith(str(p)),
    "endswith": lambda ev, s, p: _as_series(ev, s).astype(str).str.endswith(str(p)),
    "year": lambda ev, x: _to_dt(_as_series(ev, x)).dt.year,
    "month": lambda ev, x: _to_dt(_as_series(ev, x)).dt.month,
    "day": lambda ev, x: _to_dt(_as_series(ev, x)).dt.day,
    "date": lambda ev, x: _to_dt(_as_series(ev, x)),
    "least": lambda ev, *a: np.minimum.reduce([_coerce_numeric(_as_series(ev, x), ev.decimal) for x in a]),
    "greatest": lambda ev, *a: np.maximum.reduce([_coerce_numeric(_as_series(ev, x), ev.decimal) for x in a]),
}
_BINOPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
}
_CMPOPS = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
}
_FLIPPED = {ast.Eq: ast.Eq, ast.NotEq: ast.NotEq, ast.Lt: ast.Gt, ast.LtE: ast.GtE, ast.Gt: ast.Lt, ast.GtE: ast.LtE}


def _is_text_series(s):
    return isinstance(s, pd.Series) and (s.dtype == object or pd.api.types.is_string_dtype(s))


def _is_numeric_series(s, decimal=None):
    if pd.api.types.is_bool_dtype(s):
        return False
    return pd.api.types.is_numeric_dtype(s) or _is_numeric_col(s, decimal=decimal)


def _is_temporal_series(s):
    if pd.api.types.is_datetime64_any_dtype(s):
        return True
    return not pd.api.types.is_numeric_dtype(s) and _is_datetime_col(s)


def _text(s):
    """Text view of a series that keeps blanks blank (astype(str) would turn NaN into 'nan')."""
    return s.astype("string")


def _mask(m, index):
    """A clean boolean mask: blanks / NA never match."""
    if isinstance(m, pd.Series):
        return m.fillna(False).astype(bool)
    return pd.Series(bool(m), index=index)


def _scalar_number(v, decimal=None):
    """A literal as a float, or None when it is not a number (text is parsed strictly)."""
    if _is_number(v):
        try:
            return float(v)
        except OverflowError:
            raise _ExprError("number is too large") from None
    if isinstance(v, str):
        n = _coerce_numeric(pd.Series([v], dtype=object), decimal).iloc[0]
        return None if pd.isna(n) else float(n)
    return None


def _scalar_date(v):
    ts = pd.to_datetime(v, errors="coerce") if isinstance(v, str) else pd.NaT
    return None if pd.isna(ts) else ts


def _compare(s, op, v, decimal=None):
    """Typed comparison of a series with a literal (number, date, text, bool or None).

    Numbers compare numerically (the column is read with the same parsing as measures);
    text literals compare as dates on a temporal column, otherwise as exact text. A blank
    cell never satisfies ==, <, <=, >, >=, and always satisfies != (pandas semantics)."""
    if op is ast.NotEq:
        return ~_compare(s, ast.Eq, v, decimal)
    fn = _CMPOPS[op]
    if isinstance(v, (list, tuple)):
        raise _ExprError("use 'in [...]' to compare with a list")
    if v is None:
        if op is not ast.Eq:
            raise _ExprError("only == and != can compare with None")
        return s.isna()
    if isinstance(v, (bool, np.bool_)):
        if op is not ast.Eq:
            raise _ExprError("only == and != can compare with True/False")
        if pd.api.types.is_bool_dtype(s):
            return _mask(s == bool(v), s.index)
        return _mask(_text(s).str.lower() == str(bool(v)).lower(), s.index)
    if _is_number(v):
        return _mask(fn(_coerce_numeric(s, decimal), _scalar_number(v)), s.index)
    v = str(v)
    if pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s):
        num = _scalar_number(v, decimal)
        if num is None:
            if op is ast.Eq:
                return pd.Series(False, index=s.index)
            raise _ExprError(f"cannot order the numeric column against text '{v}'")
        return _mask(fn(s, num), s.index)
    ts = _scalar_date(v)
    if ts is not None and _is_temporal_series(s):
        try:
            return _mask(fn(_coerce_datetime(s), ts), s.index)
        except TypeError as e:
            raise _ExprError(f"cannot compare dates: {e}") from None
    return _mask(fn(_text(s), v), s.index)


def _compare_series(a, op, b, decimal=None):
    """Typed comparison of two series (column vs column / derived value)."""
    if op is ast.NotEq:
        return ~_compare_series(a, ast.Eq, b, decimal)
    fn = _CMPOPS[op]
    if pd.api.types.is_bool_dtype(a) and pd.api.types.is_bool_dtype(b):
        return _mask(fn(a, b), a.index)
    if _is_numeric_series(a, decimal) and _is_numeric_series(b, decimal):
        return _mask(fn(_coerce_numeric(a, decimal), _coerce_numeric(b, decimal)), a.index)
    if _is_temporal_series(a) and _is_temporal_series(b):
        return _mask(fn(_coerce_datetime(a), _coerce_datetime(b)), a.index)
    return _mask(fn(_text(a), _text(b)), a.index)


def _isin(s, values, decimal=None):
    """Typed membership test of a series against a list of literals."""
    values = values if isinstance(values, (list, tuple)) else [values]
    m = pd.Series(False, index=s.index)
    if any(v is None for v in values):
        m |= s.isna()
    bools = [bool(v) for v in values if isinstance(v, (bool, np.bool_))]
    nums = [_scalar_number(v) for v in values if _is_number(v)]
    texts = [str(v) for v in values if isinstance(v, str)]
    for b in bools:
        m |= _compare(s, ast.Eq, b, decimal)
    if pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s):
        nums += [n for n in (_scalar_number(t, decimal) for t in texts) if n is not None]
        texts = []
    if nums:
        m |= _mask(_coerce_numeric(s, decimal).isin(nums), s.index)
    if texts:
        dates = [_scalar_date(t) for t in texts]
        if all(d is not None for d in dates) and _is_temporal_series(s):
            m |= _mask(_coerce_datetime(s).isin(dates), s.index)
        else:
            m |= _mask(_text(s).isin(texts), s.index)
    return m


def _numeric_operand(x, decimal=None):
    """Arithmetic operand: text columns are read as numbers, never repeated or concatenated."""
    if isinstance(x, (list, tuple)):
        raise _ExprError("lists cannot be used in arithmetic")
    if isinstance(x, str):
        raise _ExprError(f"text '{x}' cannot be used in arithmetic (only + joins text)")
    if _is_text_series(x):
        return _coerce_numeric(x, decimal)
    return x


def _max_text_len(x):
    if isinstance(x, pd.Series):
        n = _text(x).str.len().max()
        return 0 if pd.isna(n) else int(n)
    return len(str(x)) if x is not None else 0


def _arith(op, a, b, decimal=None):
    if op is ast.Add and (isinstance(a, str) or isinstance(b, str) or (_is_text_series(a) and _is_text_series(b))):
        if isinstance(a, (list, tuple)) or isinstance(b, (list, tuple)):
            raise _ExprError("lists cannot be used in arithmetic")
        if _is_number(a) or _is_number(b):
            raise _ExprError("cannot add text and a number; wrap the number in text(...) to join them")
        if _max_text_len(a) + _max_text_len(b) > _MAX_TEXT_LEN:
            raise _ExprError(f"joined text would exceed {_MAX_TEXT_LEN} characters per cell")
        return a + b
    a, b = _numeric_operand(a, decimal), _numeric_operand(b, decimal)
    try:
        with np.errstate(all="ignore"):
            return _BINOPS[op](a, b)
    except ZeroDivisionError:
        raise _ExprError("division by zero") from None
    except OverflowError:
        raise _ExprError("number is too large") from None
    except TypeError as e:
        raise _ExprError(f"unsupported arithmetic: {e}") from None


def _const_exponent(node):
    """The exponent of ** must be a literal number (e.g. x ** 2, x ** 0.5, x ** -1)."""
    sign = 1
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        sign = -1 if isinstance(node.op, ast.USub) else 1
        node = node.operand
    if not (isinstance(node, ast.Constant) and _is_number(node.value)):
        raise _ExprError("the exponent of ** must be a literal number, e.g. x ** 2")
    exp = sign * float(node.value)
    if abs(exp) > _MAX_POW_EXPONENT:
        raise _ExprError(f"the exponent of ** must be between -{_MAX_POW_EXPONENT} and {_MAX_POW_EXPONENT}")
    return exp


class _SafeExpr:
    """Vectorized evaluation of a restricted expression against a dataframe."""

    def __init__(self, df, params=None, decimal=None):
        self.df = df
        self.params = params or {}
        self.decimal = decimal  # fallback decimal separator for numbers stored as text

    def eval(self, expr):
        return self._ev(_parse_expr(expr).body)

    def _ev(self, node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, (ast.List, ast.Tuple)):
            return [self._ev(e) for e in node.elts]
        if isinstance(node, ast.Name):
            nm = node.id
            if nm in _FUNCS:
                raise _ExprError(f"'{nm}' is a function; call it as {nm}(...)")
            c = _resolve_simple(self.df, nm)
            if c is None:
                raise _ExprError(f"unknown column '{nm}'")
            return self.df[c]
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                return self._method(node)
            return _FUNCS[node.func.id](self, *[self._ev(a) for a in node.args])
        if isinstance(node, ast.BinOp):
            if isinstance(node.op, (ast.BitAnd, ast.BitOr)):  # boolean and / or on conditions
                a, b = self._cond(node.left), self._cond(node.right)
                return (a & b) if isinstance(node.op, ast.BitAnd) else (a | b)
            if isinstance(node.op, ast.Pow):
                exp = _const_exponent(node.right)
                base = _numeric_operand(self._ev(node.left), self.decimal)
                if not isinstance(base, pd.Series) and _scalar_number(base) is None:
                    raise _ExprError("** needs a number or a numeric column")
                with np.errstate(all="ignore"):
                    if isinstance(base, pd.Series):
                        return np.power(pd.to_numeric(base, errors="coerce").astype(float), exp)
                    return float(np.power(np.float64(_scalar_number(base)), exp))
            return _arith(type(node.op), self._ev(node.left), self._ev(node.right), self.decimal)
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, (ast.Not, ast.Invert)):  # logical not (never bitwise)
                return ~self._cond(node.operand)
            v = self._ev(node.operand)
            if isinstance(node.op, ast.USub):
                return -_numeric_operand(v, self.decimal)
            return v
        if isinstance(node, ast.BoolOp):
            masks = [self._cond(v) for v in node.values]
            out = masks[0]
            for m in masks[1:]:
                out = (out & m) if isinstance(node.op, ast.And) else (out | m)
            return out
        if isinstance(node, ast.Compare):
            result, cur = None, self._ev(node.left)
            for op, comp in zip(node.ops, node.comparators, strict=True):
                right = self._ev(comp)
                if isinstance(op, (ast.In, ast.NotIn)):
                    if not isinstance(right, (list, tuple)):
                        raise _ExprError("'in' needs a list, e.g. `Region` in ['North', 'South']")
                    m = _isin(_as_series(self, cur), right, self.decimal)
                    m = ~m if isinstance(op, ast.NotIn) else m
                else:
                    m = self._compare(cur, type(op), right)
                result = m if result is None else (result & m)
                cur = right
            return result
        raise _ExprError(f"unsupported syntax: {type(node).__name__}")

    def _cond(self, node):
        """Evaluate a node used as a condition (operand of and / or / not / & / | / ~)."""
        return _mask(_to_bool(self._ev(node)), self.df.index)

    def _compare(self, a, op, b):
        a_ser, b_ser = isinstance(a, pd.Series), isinstance(b, pd.Series)
        if a_ser and b_ser:
            return _compare_series(a, op, b, self.decimal)
        if a_ser:
            return _compare(a, op, b, self.decimal)
        if b_ser:
            return _compare(b, _FLIPPED[op], a, self.decimal)
        try:  # literal vs literal
            return _mask(_CMPOPS[op](a, b), self.df.index)
        except TypeError as e:
            raise _ExprError(f"cannot compare {a!r} with {b!r}: {e}") from None

    def _method(self, node):
        """Dispatch an allow-listed pandas-style column method (see _METHODS / _STR_METHODS)."""
        func, name = node.func, node.func.attr
        target = self._ev(func.value.value if name in _STR_METHODS else func.value)
        s = _as_series(self, target)
        args = [self._ev(a) for a in node.args]
        kw = {k.arg: self._ev(k.value) for k in node.keywords}

        def want(n_min, n_max):
            if not n_min <= len(args) <= n_max:
                raise _ExprError(f".{name}() takes {n_min if n_min == n_max else f'{n_min}-{n_max}'} argument(s)")

        if name in ("isna", "isnull"):
            want(0, 0)
            return s.isna()
        if name in ("notna", "notnull"):
            want(0, 0)
            return s.notna()
        if name == "isin":
            want(1, 1)
            if not isinstance(args[0], (list, tuple)):
                raise _ExprError(".isin() needs a list, e.g. .isin(['North', 'South'])")
            return _isin(s, args[0], self.decimal)
        if name == "between":
            want(2, 3)
            inclusive = kw.get("inclusive", args[2] if len(args) == 3 else "both")
            if inclusive not in ("both", "neither", "left", "right"):
                raise _ExprError("inclusive must be 'both', 'neither', 'left' or 'right'")
            lo, hi = args[0], args[1]
            if _is_number(lo) and _is_number(hi):
                x, lo, hi = _coerce_numeric(s, self.decimal), _scalar_number(lo), _scalar_number(hi)
            elif isinstance(lo, str) and isinstance(hi, str):
                dlo, dhi = _scalar_date(lo), _scalar_date(hi)
                if dlo is not None and dhi is not None and _is_temporal_series(s):
                    x, lo, hi = _coerce_datetime(s), dlo, dhi
                elif pd.api.types.is_numeric_dtype(s) and None not in (_scalar_number(lo), _scalar_number(hi)):
                    x, lo, hi = s, _scalar_number(lo), _scalar_number(hi)
                else:
                    x = _text(s)
            else:
                raise _ExprError(".between() needs two numbers or two texts/dates")
            return _mask(x.between(lo, hi, inclusive=inclusive), s.index)
        text = _text(s)
        transforms = {"lower": text.str.lower, "upper": text.str.upper, "strip": text.str.strip, "len": text.str.len}
        if name in transforms:
            want(0, 0)
            return transforms[name]()
        if name == "contains":
            want(1, 1)
            if kw.get("regex") not in (None, False):
                raise _ExprError(".str.contains() matches literal text; regular expressions are not supported")
            if not isinstance(kw.get("case", True), bool):
                raise _ExprError("case must be True or False")
            sub = args[0]
            if not isinstance(sub, str):
                raise _ExprError(".str.contains() needs text, e.g. .str.contains('north')")
            return _mask(text.str.contains(sub, case=bool(kw.get("case", True)), regex=False), s.index)
        want(1, 1)  # startswith / endswith
        pats = args[0] if isinstance(args[0], (list, tuple)) else [args[0]]
        if not pats or not all(isinstance(p, str) for p in pats):
            raise _ExprError(f".str.{name}() needs text or a list of texts")
        m = pd.Series(False, index=s.index)
        for p in pats:
            m |= _mask(text.str.startswith(p) if name == "startswith" else text.str.endswith(p), s.index)
        return m


# --------------------------------------------------------------------------- #
# Transform pipeline
# --------------------------------------------------------------------------- #
def _resolve_each(df, names):
    """Each name resolved like _resolve_simple; unknown names are kept for pandas to report."""
    return [_resolve_simple(df, n) or n for n in names]


def _parse_aggspec(spec):
    if isinstance(spec, str) and ":" in spec:
        fn, col = spec.split(":", 1)
        return fn.strip().lower(), col.strip()
    return str(spec).strip().lower(), None


def _do_group(df, by, aggspec, decimal=None):
    cols = {}
    for outname, spec in (aggspec or {"count": "count"}).items():
        fn, col = _parse_aggspec(spec)
        if fn not in _AGG_FUNCS:
            raise _ExprError(f"group: unknown aggregation '{fn}'. Allowed: {', '.join(_AGG_FUNCS)}")
        if fn == "count" and not col:
            cols[outname] = df.groupby(by, dropna=False).size() if by else pd.Series({"_": len(df)})
            continue
        rc = _resolve_simple(df, col) if col else None
        if rc is None:
            raise _ExprError(f"group: unknown column '{col}'")
        v = df[rc] if fn in ("nunique", "count") else _coerce_numeric(df[rc], decimal)
        if by:
            tmp = pd.DataFrame({**{b: df[b] for b in by}, "_v": v})
            cols[outname] = tmp.groupby(by, dropna=False)["_v"].agg(fn)
        else:
            cols[outname] = pd.Series({"_": _SERIES_AGGS[fn](v)})
    res = pd.DataFrame(cols)
    return res.reset_index() if by else res.reset_index(drop=True)


def _do_window(df, step, decimal=None):
    fn = (step.get("fn") or "").lower()
    name = step.get("name") or fn
    by = _resolve_each(df, step.get("by") or [])
    w = df.copy()
    order_by = step.get("order_by")
    if order_by:
        ob = _resolve_each(w, order_by if isinstance(order_by, list) else [order_by])
        w = w.sort_values(ob, ascending=(str(step.get("dir", "asc")).lower() != "desc"))
    rc = _resolve_simple(w, step.get("value")) if step.get("value") else None
    if not rc:
        raise _ExprError("window: 'value' column required")
    col = _coerce_numeric(w[rc], decimal)
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


def _do_pivot(df, step, decimal=None, max_cells=None):
    index = step.get("index")
    idx = _resolve_each(df, index if isinstance(index, list) else [index])
    colc = _resolve_simple(df, step.get("columns"))
    valc = _resolve_simple(df, step.get("values"))
    if not colc or not valc:
        raise _ExprError("pivot needs index, columns, values")
    pivot_agg = str(step.get("agg", "sum")).strip().lower()
    if pivot_agg not in _AGG_FUNCS:
        raise _ExprError(f"pivot: unknown aggregation '{pivot_agg}'. Allowed: {', '.join(_AGG_FUNCS)}")
    cells = len(df[idx].drop_duplicates()) * int(df[colc].nunique(dropna=False))
    if max_cells and cells > max_cells:
        raise _ExprError(
            f"pivot would have {cells:,} cells, above max_cells={int(max_cells):,}; "
            "use lower-cardinality index/columns or filter first"
        )
    out = pd.pivot_table(
        pd.DataFrame({**{c: df[c] for c in idx}, colc: df[colc], valc: _coerce_numeric(df[valc], decimal)}),
        index=idx,
        columns=colc,
        values=valc,
        aggfunc=pivot_agg,
        fill_value=0,
    ).reset_index()
    out.columns = [str(c) for c in out.columns]
    return out


_PIPE_OPS = {
    "select",
    "filter",
    "derive",
    "group",
    "aggregate",
    "sort",
    "limit",
    "distinct",
    "window",
    "pivot",
    "unpivot",
}
_MAX_PIPELINE_STEPS = 50


def _run_pipeline(df, pipeline, params, decimal=None, max_cells=None):
    if len(pipeline) > _MAX_PIPELINE_STEPS:
        raise _ExprError(f"a pipeline may have at most {_MAX_PIPELINE_STEPS} steps")
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
            mask = _SafeExpr(work, params, decimal).eval(step.get("expr", ""))
            if isinstance(mask, pd.Series):
                work = work[_to_bool(mask).reindex(work.index, fill_value=False)]
            elif not _to_bool(mask):
                work = work.iloc[0:0]
        elif op == "derive":
            name = step.get("name")
            if not name:
                raise _ExprError(f"step {i + 1}: derive needs 'name'")
            val = _SafeExpr(work, params, decimal).eval(step.get("expr", ""))
            work = work.copy()
            work[name] = _as_series_like(val, work)
            if name not in derived:
                derived.append(name)
        elif op == "select":
            cols = [c for c in _resolve_each(work, step.get("columns") or []) if c in work.columns]
            if not cols:
                raise _ExprError(f"step {i + 1}: select found none of the requested columns")
            work = work[cols]
        elif op in ("group", "aggregate"):
            work = _do_group(work, _resolve_each(work, step.get("by") or []), step.get("agg"), decimal)
        elif op == "sort":
            by = step.get("by")
            by = _resolve_each(work, by if isinstance(by, list) else [by])
            d = step.get("dir", "asc")
            asc = [str(x).lower() != "desc" for x in (d if isinstance(d, list) else [d] * len(by))]
            work = work.sort_values(by, ascending=asc)
        elif op == "limit":
            work = work.head(int(step.get("n", 10)))
        elif op == "distinct":
            cols = step.get("columns")
            work = work.drop_duplicates(subset=_resolve_each(work, cols) if cols else None)
        elif op == "window":
            work = _do_window(work, step, decimal)
        elif op == "pivot":
            work = _do_pivot(work, step, decimal, max_cells)
        elif op == "unpivot":
            vv = step.get("value_vars")
            work = work.melt(
                id_vars=_resolve_each(work, step.get("id_vars") or []),
                value_vars=_resolve_each(work, vv) if vv else None,
                var_name=step.get("var_name", "variable"),
                value_name=step.get("value_name", "value"),
            )
        steps_log.append({"step": i + 1, "op": op, "rows_in": int(before), "rows_out": int(len(work))})
    return work, derived, steps_log


# --------------------------------------------------------------------------- #
# Operations of analyze_spreadsheet
# --------------------------------------------------------------------------- #
_BLANK = "(blank)"


def _group_keys(s: pd.Series) -> pd.Series:
    """Group labels with blank cells (missing or whitespace-only) as an explicit '(blank)'
    group. pandas drops missing keys from groupby / pivot_table by default, and the groups
    would then no longer add up to the column total."""
    blank = s.isna() | s.map(lambda v: isinstance(v, str) and not v.strip()).astype(bool)
    return s.astype(object).where(~blank, _BLANK)


def _apply_aggs(series: pd.Series, aggs: list) -> dict:
    """{agg: value} for several allow-listed aggregations of one series (None where one fails)."""
    out = {}
    for a in aggs:
        try:
            out[a] = round(float(_SERIES_AGGS[a](series)), 4)
        except Exception:
            out[a] = None
    return out


def _records_to_md(records):
    """Row dicts as a markdown table ('|' escaped, newlines flattened). The model is told to
    show this table as-is rather than retype the rows."""
    if not isinstance(records, list) or not records or not isinstance(records[0], dict):
        return ""
    cols = list(dict.fromkeys(k for r in records for k in r))

    def _cell(v):
        if v is None or (isinstance(v, float) and v != v):  # None / NaN
            return ""
        return str(v).replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(str(c) for c in cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    for r in records:
        lines.append("| " + " | ".join(_cell(r.get(c)) for c in cols) + " |")
    return "\n".join(lines)


def _columns_with_role(roles, *wanted):
    return [c for c, r in roles.items() if r["role"] in wanted]


def _apply_filters(df, filters, params, decimal):
    """(rows matching `filters`, None), or (None, error result) when the expression is rejected.
    `filters` goes through the safe expression grammar, never DataFrame.query / eval."""
    try:
        mask = _SafeExpr(df, params or {}, decimal).eval(filters)
        is_mask = isinstance(mask, pd.Series) and pd.api.types.is_bool_dtype(mask)
        if not is_mask and not isinstance(mask, (bool, np.bool_)):
            raise _ExprError("filters must be a condition, e.g. `Units` > 20 and `Region` == 'North'")
        return df[_mask(mask, df.index)], None
    except Exception as e:
        reason = str(e) if isinstance(e, _ExprError) else f"{type(e).__name__}: {e}"
        return None, {
            "status": "error",
            "reason": f"filter error: {reason}",
            "hint": _EXPR_HINT,
            "columns_available": list(df.columns),
        }


class _Analysis:
    """One analyze_spreadsheet call. Each public method runs the operation of the same name on
    the filtered rows and returns the result; it receives every tool argument as a keyword and
    names the ones it uses."""

    def __init__(self, valves, cache, unfiltered, df, info, prov):
        self.valves, self.cache, self.info, self.prov = valves, cache, info, prov
        self.unfiltered = unfiltered  # measures are parsed on the whole column
        self.df = df
        self.decimal = info["decimal"]
        self.roles = info["roles"]
        self.measures = _columns_with_role(self.roles, "measure")
        self.dims = _columns_with_role(self.roles, "dimension", "identifier")
        self.temporal = _columns_with_role(self.roles, "temporal")
        self.cap = max(int(valves.max_return_rows), 1)  # detail rows any operation returns

    # -- column arguments -------------------------------------------------- #
    def need_column(self, name, what="Column"):
        """Resolve any column argument; never guess (ask, listing the real columns)."""
        col, cand = _resolve_col(self.df, name, self.valves.ambiguity_min_gap)
        if col:
            return col, None
        return None, {
            "status": "needs_clarification",
            "reason": f"{what} '{name}' not found (no column matches it closely enough to use without asking).",
            "candidates": cand,
            "columns_available": list(self.df.columns),
        }

    def need_measure(self, col):
        """Resolve/validate a measure column; clarify if ambiguous."""
        if col:
            resolved, cand = _resolve_col(self.df, col, self.valves.ambiguity_min_gap)
            if resolved:
                s = self.unfiltered[resolved]
                n_numbers, n_values = int(self.parsed(resolved)[0].notna().sum()), int(s.notna().sum())
                if not pd.api.types.is_numeric_dtype(s) and (not n_values or n_numbers / n_values < 0.8):
                    return None, {
                        "status": "needs_clarification",
                        "reason": f"Column '{resolved}' is not numeric: only {n_numbers} of "
                        f"{n_values} non-blank values are numbers.",
                        "candidates": self.measures,
                    }
                return resolved, None
            return None, {
                "status": "needs_clarification",
                "reason": f"Column '{col}' not found (no column matches it closely enough to use without asking).",
                "candidates": cand or self.measures,
                "columns_available": list(self.df.columns),
            }
        if len(self.measures) == 1:
            return self.measures[0], None
        if len(self.measures) == 0:
            return None, {"status": "error", "reason": "No numeric measure column detected."}
        return None, {
            "status": "needs_clarification",
            "reason": "Multiple numeric columns could be the measure. Which one?",
            "candidates": self.measures,
        }

    def parsed(self, col):
        """(numbers, format info) of a whole column, parsed once per cached file and column."""
        numbers = self.info["numbers"]
        if col not in numbers:
            numbers[col] = _parse_numbers(self.unfiltered[col], self.decimal)
            self.cache.charge(
                self.info["cache_key"], int(numbers[col][0].memory_usage(index=False)), *_cache_limits(self.valves)
            )
        return numbers[col]

    def num(self, col):
        """Column as numbers. Parsed on the whole column, so a filter cannot change how a
        value like '1,234' is read; the decimal separator used is reported in provenance."""
        values, fmt = self.parsed(col)
        if fmt:
            fmt = dict(fmt)
            if fmt.get("decided_by") == "fallback":
                fmt["decided_by"] = self.info["decimal_decided_by"]
            self.prov.setdefault("number_format", {})[col] = fmt
        return values.reindex(self.df.index)

    def _per_period(self, date_col, values, fq, agg):
        """`agg` of `values` per period of `date_col`; rows without a date are left out."""
        t = pd.DataFrame({"d": _coerce_datetime(self.df[date_col]), "v": values}).dropna(subset=["d"])
        return t.set_index("d")["v"].resample(fq).agg(agg), int(len(t))

    # -- operations -------------------------------------------------------- #
    def overview(self, **_):
        df, roles, prov = self.df, self.roles, self.prov
        ov = {
            "rows": len(self.df),
            "columns": list(df.columns),
            "roles": {c: roles[c]["role"] for c in df.columns},
            "missing_per_column": {c: int(df[c].isna().sum()) for c in df.columns if df[c].isna().sum()},
            "duplicate_full_rows": int(df.duplicated().sum()),
            "currency_detected": _detect_currency(df),
            "measures": {},
            "top_dimensions": {},
        }
        for m in self.measures:
            col = self.num(m)
            ov["measures"][m] = {
                "sum": round(float(col.sum()), 4),
                "mean": round(float(col.mean()), 4),
                "min": round(float(col.min()), 4),
                "max": round(float(col.max()), 4),
                "non_null": int(col.notna().sum()),
            }
        for d in self.dims[:5]:
            ov["top_dimensions"][d] = {str(k): int(v) for k, v in df[d].value_counts().head(5).items()}
        if self.temporal:
            dt = _coerce_datetime(df[self.temporal[0]])
            ov["date_range"] = {"column": self.temporal[0], "min": str(dt.min()), "max": str(dt.max())}
        prov.update({"columns_used": list(df.columns), "rows_processed": len(self.df)})
        return {"status": "ok", "result": ov, "provenance": prov}

    def aggregate(self, value_column, group_by, agg, aggs, n, **_):
        if group_by:
            return self.group_aggregate(value_column, group_by, agg, aggs, n)
        df, prov = self.df, self.prov
        if agg == "count":  # counts any column (or the rows), no measure needed
            if value_column:
                col, err = self.need_column(value_column)
                if err:
                    return err
                cnt = int(df[col].notna().sum())
            else:
                col, cnt = "(rows)", len(self.df)
            prov.update(
                {
                    "columns_used": {"column": col},
                    "agg": "count",
                    "rows_processed": len(self.df),
                    "code": (f"df['{col}'].notna().sum()" if col != "(rows)" else "len(df)"),
                }
            )
            return {"status": "ok", "result": {"count": cnt, "column": col}, "provenance": prov}
        mcol, err = self.need_measure(value_column)
        if err:
            return err
        series = self.num(mcol)
        processed = int(series.notna().sum())
        agg_list = list(dict.fromkeys(aggs))
        if agg_list:
            res = _apply_aggs(series, agg_list)
            prov.update(
                {
                    "columns_used": {"value": mcol},
                    "agg": agg_list,
                    "rows_processed": processed,
                    "code": f"df['{mcol}'].agg({agg_list})",
                }
            )
            return {"status": "ok", "result": {**res, "value_column": mcol}, "provenance": prov}
        val = float(_SERIES_AGGS[agg](series))
        prov.update(
            {"columns_used": {"value": mcol}, "agg": agg, "rows_processed": processed, "code": f"df['{mcol}'].{agg}()"}
        )
        return {"status": "ok", "result": {agg: round(val, 4), "value_column": mcol}, "provenance": prov}

    def top_n(self, value_column, group_by, agg, aggs, n, **_):
        return self.group_aggregate(value_column, group_by, agg, aggs, n, top=True)

    def group_aggregate(self, value_column, group_by, agg, aggs, n, top=False, **_):
        df, prov = self.df, self.prov
        mcol, err = self.need_measure(value_column)
        if err:
            return err
        series = self.num(mcol)
        processed = int(series.notna().sum())
        agg_list = list(dict.fromkeys(aggs))
        if not group_by:
            return {
                "status": "needs_clarification",
                "reason": "group_by column required for this operation.",
                "candidates": self.dims,
            }
        gcol, err = self.need_column(group_by, "group column")
        if err:
            return err
        # sum is always computed (shares / totals); groups are ranked by the requested agg
        pandas_aggs = list(dict.fromkeys(["sum"] + (agg_list or ["sum", "count", "mean"]) + [agg]))
        tmp = pd.DataFrame({gcol: _group_keys(df[gcol]), mcol: series})
        grouped = tmp.groupby(gcol, sort=False)[mcol].agg(pandas_aggs)
        grand_total = float(grouped["sum"].sum())
        grouped["pct_of_total"] = grouped["sum"] / (grand_total or 1) * 100
        grouped = grouped.sort_values(agg, ascending=False).round(4)
        groups_total = int(len(grouped))
        if top:
            grouped = grouped.head(max(int(n), 1))
        returned = grouped.head(self.cap).reset_index().to_dict(orient="records")
        returned_subtotal = round(float(sum((r.get("sum") or 0) for r in returned)), 4)
        summary = {
            "grand_total": round(grand_total, 4),
            "groups_total": groups_total,
            "groups_returned": len(returned),
            "returned_subtotal": returned_subtotal,
            "returned_share_of_total_pct": round(returned_subtotal / (grand_total or 1) * 100, 4),
            "truncated": len(grouped) > len(returned),
            "blank_group_rows": int((tmp[gcol] == _BLANK).sum()),
        }
        prov.update(
            {
                "columns_used": {"value": mcol, "group": gcol},
                "agg": pandas_aggs,
                "ranked_by": agg,
                "rows_processed": processed,
                "code": f"df.groupby('{gcol}', dropna=False)['{mcol}'].agg({pandas_aggs})",
            }
        )
        return {"status": "ok", "result": returned, "summary": summary, "provenance": prov}

    def value_counts(self, value_column, group_by, n, **_):
        df, prov = self.df, self.prov
        if not (value_column or group_by):
            return {"status": "needs_clarification", "reason": "Which column to count?", "candidates": list(df.columns)}
        col, err = self.need_column(value_column or group_by)
        if err:
            return err
        counts = df[col].value_counts()
        vc = counts.head(min(max(int(n), 1), self.cap))
        total = int(df[col].notna().sum())
        prov.update({"columns_used": {"column": col}, "rows_processed": total, "code": f"df['{col}'].value_counts()"})
        return {
            "status": "ok",
            "result": {
                "counts": {str(k): int(v) for k, v in vc.items()},
                "percent": {str(k): round(v / (total or 1) * 100, 2) for k, v in vc.items()},
                "total": total,
                "blank_cells": int(len(df) - total),
                "distinct_values": int(len(counts)),
                "truncated": len(counts) > len(vc),
            },
            "provenance": prov,
        }

    def describe(self, columns, **_):
        df, prov = self.df, self.prov
        cols = []
        for name in columns or self.measures or list(df.select_dtypes(include=np.number).columns):
            c, err = self.need_column(name)
            if err:
                return err
            cols.append(c)
        desc = {c: {k: round(float(v), 4) for k, v in self.num(c).describe().items()} for c in cols}
        prov.update({"columns_used": cols, "rows_processed": len(self.df), "code": "df[cols].describe()"})
        return {"status": "ok", "result": desc, "provenance": prov}

    def time_series(self, value_column, date_column, freq, agg, **_):
        prov = self.prov
        if not date_column:
            if len(self.temporal) != 1:
                return {"status": "needs_clarification", "reason": "Which date column?", "candidates": self.temporal}
            date_column = self.temporal[0]
        date_column, err = self.need_column(date_column, "date column")
        if err:
            return err
        mcol, err = self.need_measure(value_column)
        if err:
            return err
        fq = _FREQ_ALIASES.get(str(freq).strip().upper())
        if fq is None:
            return {"status": "error", "reason": f"Unknown freq '{freq}'.", "supported": _FREQ_CHOICES}
        ts, rows = self._per_period(date_column, self.num(mcol), fq, agg)
        prov.update(
            {
                "columns_used": {"date": date_column, "value": mcol},
                "freq": fq,
                "rows_processed": rows,
                "code": f"df.set_index('{date_column}')['{mcol}'].resample('{fq}').{agg}()",
            }
        )
        periods = [(str(k.date()), round(float(v), 4)) for k, v in ts.items()]
        return {
            "status": "ok",
            "result": dict(periods[: self.cap]),
            "periods_total": len(periods),
            "truncated": len(periods) > self.cap,
            "provenance": prov,
        }

    def anomalies(self, value_column, method, threshold, **_):
        df, prov = self.df, self.prov
        mcol, err = self.need_measure(value_column)
        if err:
            return err
        if method not in _ANOMALY_METHODS:
            return {
                "status": "error",
                "reason": f"Unknown anomaly method '{method}'.",
                "supported": list(_ANOMALY_METHODS),
            }
        s = self.num(mcol)
        if method == "zscore":
            mu, sd = s.mean(), (s.std() or 1)
            mask = ((s - mu) / sd).abs() >= threshold
            bounds = {"lower": round(float(mu - threshold * sd), 4), "upper": round(float(mu + threshold * sd), 4)}
        elif method == "pct_above_mean":
            mask = s >= s.mean() * (1 + threshold)
            bounds = {"threshold_value": round(float(s.mean() * (1 + threshold)), 4)}
        else:  # iqr
            q1, q3 = s.quantile(0.25), s.quantile(0.75)
            iqr = q3 - q1
            mask = (s < q1 - threshold * iqr) | (s > q3 + threshold * iqr)
            bounds = {"lower": round(float(q1 - threshold * iqr), 4), "upper": round(float(q3 + threshold * iqr), 4)}
        mask = mask.fillna(False)
        flagged = df[mask].copy()
        flagged[mcol] = s[mask]
        total_flagged = int(mask.sum())
        flagged_sum = round(float(s[mask].sum()), 4)
        total_sum = float(s.sum())
        # counts of the flagged rows per dimension, so the model never counts rows itself
        breakdown = {}
        for d in self.dims:
            if d in flagged.columns:
                vc = _group_keys(flagged[d]).value_counts().head(20)
                breakdown[d] = {str(k): int(v) for k, v in vc.items()}
        rows_out = flagged.head(self.cap).to_dict(orient="records")
        prov.update(
            {
                "columns_used": {"value": mcol},
                "method": method,
                "threshold": threshold,
                "rows_processed": int(s.notna().sum()),
                "code": f"anomaly({method}, thr={threshold}) on '{mcol}'",
            }
        )
        return {
            "status": "ok",
            "result": {
                "anomaly_count": total_flagged,
                "mean": round(float(s.mean()), 4),
                "bounds": bounds,
                "flagged_sum": flagged_sum,
                "flagged_share_of_total_pct": round(flagged_sum / (total_sum or 1) * 100, 4),
                "breakdown": breakdown,
                "rows_returned": len(rows_out),
                "truncated": total_flagged > len(rows_out),
                "rows": rows_out,
            },
            "provenance": prov,
        }

    def duplicates(self, subset, **_):
        df, prov = self.df, self.prov
        res = {"duplicate_full_rows": int(df.duplicated().sum())}
        if subset:
            key = []
            for name in subset:
                c, err = self.need_column(name)
                if err:
                    return err
                key.append(c)
            res["duplicate_by_key"] = int(df.duplicated(subset=key).sum())
            res["key"] = key
        prov.update(
            {
                "columns_used": subset or "all",
                "rows_processed": len(self.df),
                "code": f"df.duplicated(subset={subset})",
            }
        )
        return {"status": "ok", "result": res, "provenance": prov}

    def missing(self, **_):
        df, prov = self.df, self.prov
        miss = {c: int(df[c].isna().sum()) for c in df.columns}
        prov.update({"columns_used": list(df.columns), "rows_processed": len(self.df), "code": "df.isna().sum()"})
        return {"status": "ok", "result": miss, "provenance": prov}

    def pivot(self, value_column, group_by, columns, agg, **_):
        df, prov = self.df, self.prov
        mcol, err = self.need_measure(value_column)
        if err:
            return err
        if not group_by or not columns:
            return {
                "status": "needs_clarification",
                "reason": "pivot needs group_by (rows) and columns (cols).",
                "candidates": self.dims,
            }
        rcol, err = self.need_column(group_by, "row column")
        if err:
            return err
        ccol, err = self.need_column(columns[0] if isinstance(columns, list) else columns, "column dimension")
        if err:
            return err
        max_cells = int(self.valves.max_cells)
        cells = int(df[rcol].nunique(dropna=False)) * int(df[ccol].nunique(dropna=False))
        if cells > max_cells:
            return {
                "status": "error",
                "reason": f"The pivot would have {cells:,} cells, above max_cells={max_cells:,}. Use "
                "lower-cardinality row/column dimensions or filter first.",
            }
        pv = pd.pivot_table(
            pd.DataFrame({rcol: _group_keys(df[rcol]), ccol: _group_keys(df[ccol]), mcol: self.num(mcol)}),
            index=rcol,
            columns=ccol,
            values=mcol,
            aggfunc=agg,
            fill_value=0,
            sort=False,
        ).round(4)
        pv.columns = [str(c) for c in pv.columns]
        prov.update(
            {
                "columns_used": {"value": mcol, "rows": rcol, "cols": ccol},
                "agg": agg,
                "rows_processed": len(self.df),
                "code": f"pivot_table(index={rcol}, columns={ccol}, values={mcol}, aggfunc={agg})",
            }
        )
        records = pv.reset_index().to_dict(orient="records")
        return {
            "status": "ok",
            "result": records[: self.cap],
            "rows_in_result": len(records),
            "truncated": len(records) > self.cap,
            "provenance": prov,
        }

    def transform(self, pipeline, params, **_):
        df, prov = self.df, self.prov
        if not isinstance(pipeline, list) or not pipeline:
            return {
                "status": "needs_clarification",
                "reason": "transform requires a non-empty 'pipeline' list of steps "
                "(ops: select, filter, derive, group, sort, limit, distinct, window, pivot, unpivot).",
                "columns_available": list(df.columns),
            }
        try:
            work, derived, steps_log = _run_pipeline(
                df, pipeline, params or {}, self.decimal, int(self.valves.max_cells)
            )
        except _ExprError as e:
            return {
                "status": "error",
                "reason": f"pipeline error: {e}",
                "hint": _EXPR_HINT,
                "columns_available": list(df.columns),
            }
        except Exception as e:
            return {"status": "error", "reason": f"{type(e).__name__}: {e}", "columns_available": list(df.columns)}
        rows_out = work.head(self.cap).to_dict(orient="records")
        prov.update(
            {
                "pipeline": pipeline,
                "rows_processed": len(self.df),
                "steps": steps_log,
                "code": "composable transform pipeline",
            }
        )
        return {
            "status": "ok",
            "result": rows_out,
            "columns": list(work.columns),
            "derived_columns": derived,
            "rows_in_result": int(len(work)),
            "rows_returned": len(rows_out),
            "truncated": int(len(work)) > len(rows_out),
            "table_markdown": _records_to_md(rows_out),
            "provenance": prov,
        }

    def compare(self, value_column, group_by, date_column, freq, agg, subset, **_):
        mcol, err = self.need_measure(value_column)
        if err:
            return err
        series = self.num(mcol)
        if (date_column or len(self.temporal) == 1) and not group_by:
            return self._compare_periods(mcol, series, date_column or self.temporal[0], freq, agg)
        if group_by:
            return self._compare_segments(mcol, series, group_by, subset)
        return {
            "status": "needs_clarification",
            "reason": "compare needs either a date_column (period-over-period) or a group_by (segment comparison).",
            "candidates": {"temporal": self.temporal, "dimensions": self.dims},
        }

    def _compare_periods(self, mcol, series, date_column, freq, agg):
        dc, err = self.need_column(date_column, "date column")
        if err:
            return err
        fq = _FREQ_ALIASES.get(str(freq).strip().upper())
        if fq is None:
            return {"status": "error", "reason": f"Unknown freq '{freq}'.", "supported": _FREQ_CHOICES}
        ts, rows = self._per_period(dc, series, fq, agg)
        recs, prev = [], None
        for k, v in ts.items():
            v = float(v)
            chg = (v - prev) if prev is not None else None
            pct = ((v - prev) / prev * 100) if (prev not in (None, 0)) else None
            recs.append(
                {
                    "period": str(k.date() if hasattr(k, "date") else k),
                    agg: round(v, 4),
                    "change": (round(chg, 4) if chg is not None else None),
                    "pct_change": (round(pct, 4) if pct is not None else None),
                }
            )
            prev = v
        summary = {}
        if len(recs) >= 2:
            first, last = recs[0][agg], recs[-1][agg]
            summary = {
                "periods": len(recs),
                "first": first,
                "last": last,
                "net_change": round(last - first, 4),
                "net_pct_change": round((last - first) / (first or 1) * 100, 4),
            }
        self.prov.update(
            {
                "columns_used": {"date": dc, "value": mcol},
                "freq": fq,
                "agg": agg,
                "rows_processed": rows,
                "code": f"resample('{fq}').{agg}() + period-over-period change",
            }
        )
        return {
            "status": "ok",
            "result": recs[: self.cap],
            "summary": summary,
            "truncated": len(recs) > self.cap,
            "provenance": self.prov,
        }

    def _compare_segments(self, mcol, series, group_by, subset):
        gcol, err = self.need_column(group_by, "group column")
        if err:
            return err
        tmp = pd.DataFrame({gcol: _group_keys(self.df[gcol]), mcol: series})
        g = tmp.groupby(gcol, sort=False)[mcol].agg(["sum", "count", "mean"])
        if subset:
            g = g[g.index.astype(str).isin([str(x) for x in subset])]
        g = g.sort_values("sum", ascending=False).round(4)
        recs = g.reset_index().to_dict(orient="records")
        if len(recs) >= 2:
            top_sum = recs[0].get("sum") or 0
            for r in recs[1:]:
                r["delta_sum_vs_top"] = round((r.get("sum") or 0) - top_sum, 4)
                r["pct_vs_top"] = round(((r.get("sum") or 0) - top_sum) / (recs[0].get("sum") or 1) * 100, 4)
        self.prov.update(
            {
                "columns_used": {"value": mcol, "group": gcol},
                "rows_processed": int(series.notna().sum()),
                "code": f"compare groups of '{gcol}' on '{mcol}'",
            }
        )
        return {
            "status": "ok",
            "result": recs[: self.cap],
            "groups_total": len(recs),
            "truncated": len(recs) > self.cap,
            "provenance": self.prov,
        }


# The operations analyze_spreadsheet accepts, dispatched through this table (never getattr).
_OPERATIONS = {
    "overview": _Analysis.overview,
    "aggregate": _Analysis.aggregate,
    "group_aggregate": _Analysis.group_aggregate,
    "top_n": _Analysis.top_n,
    "pivot": _Analysis.pivot,
    "value_counts": _Analysis.value_counts,
    "describe": _Analysis.describe,
    "time_series": _Analysis.time_series,
    "anomalies": _Analysis.anomalies,
    "duplicates": _Analysis.duplicates,
    "missing": _Analysis.missing,
    "compare": _Analysis.compare,
    "transform": _Analysis.transform,
}


# --------------------------------------------------------------------------- #
# The tool. Open WebUI turns each method of Tools into a function the model can call (0.5 and
# 0.6 include _private methods too), so every helper lives outside the class.
# --------------------------------------------------------------------------- #
class Tools:
    class Valves(BaseModel):
        max_cells: int = Field(
            5_000_000,
            description="Refuse files larger than this (rows*cols). Enforced while reading: at most "
            "max_cells / columns rows are parsed, so an oversized file is never fully loaded.",
        )
        max_file_mb: float = Field(
            100, description="Refuse uploads larger than this many megabytes before reading them."
        )
        max_uncompressed_mb: float = Field(
            500,
            description="Excel workbooks are ZIP archives: refuse one whose contents would expand beyond "
            "this many megabytes, or that has a part compressed more than 100:1 (a zip bomb).",
        )
        ambiguity_min_gap: float = Field(
            0.15,
            description="If the best two fuzzy matches for a column name are closer than this "
            "(by score), ask which column was meant instead of guessing.",
        )
        max_return_rows: int = Field(
            1000,
            description="Most rows, groups or periods an operation returns. Longer results are cut and "
            "flagged 'truncated'; totals and summaries still cover every row.",
        )
        cache_max_entries: int = Field(
            8,
            description="Parsed spreadsheets kept in memory between calls, so follow-up questions do "
            "not re-read the file (0 disables the cache). Entries are only used after the file-access check.",
        )
        cache_max_mb: float = Field(
            512, description="Memory budget for the cache of parsed spreadsheets, in megabytes."
        )
        decimal_separator: str = Field(
            "auto",
            description="Decimal separator for numbers stored as text. Each column is read with the "
            "separator its unambiguous values show ('1.234,56' or '1,5' -> ','; '1,234.56' or '0.25' "
            "-> '.'). Only a column whose values could be read either way ('1,234') uses this setting: "
            "'auto' (default) follows the rest of the file, then '.'; or set '.' or ','. The choice is "
            "reported in each result's provenance (number_format).",
        )

    def __init__(self):
        self.valves = self.Valves()
        self._cache = _FrameCache()

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
        df, info = await _load(self._cache, self.valves, file_id, __files__, __metadata__, __user__, sheet)
        if df is None:
            return json.dumps(info)
        roles = info["roles"]
        out = {
            "file": os.path.basename(info["path"]),
            "sheets": info["sheets"],
            "active_sheet": info["sheet"],
            "rows": int(len(df)),
            "columns": list(df.columns),
            "currency_detected": _detect_currency(df),
            "number_format": {"decimal_separator_fallback": info["decimal"], "decided_by": info["decimal_decided_by"]},
            "schema": roles,
            "measures": _columns_with_role(roles, "measure"),
            "dimensions": _columns_with_role(roles, "dimension"),
            "temporal": _columns_with_role(roles, "temporal"),
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
            % of total, sorted descending. Blank group values form a "(blank)" group, so the
            groups always add up to the whole-column total.
          - "top_n": top n groups of group_by ranked by agg of value_column.
          - "pivot": value_column aggregated by group_by (rows) x columns (cols).
          - "value_counts": frequency of each value in value_column (a dimension).
          - "describe": descriptive stats for the given columns (or all measures).
          - "time_series": value_column aggregated (agg) by date_column resampled at freq
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
            isnull, notnull, least, greatest; column methods: .isna() .notna() .between(a, b)
            .isin([...]) .str.contains('x') .str.startswith('x') .str.endswith('x')
            .str.lower() .str.upper() .str.strip() .str.len(). NO arbitrary code is possible.
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
        :param filters: optional row condition applied before computing, in the same safe expression language as a transform filter step, e.g. `Region` == 'North' and `Units` > 20 (not a pandas query; no code)
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
        agg = str(agg or "sum").strip().lower()
        aggs = [str(a).strip().lower() for a in (aggs or [])]
        unknown = [a for a in [agg] + aggs if a not in _AGG_FUNCS]
        if unknown:
            return json.dumps(
                {"status": "error", "reason": f"Unknown aggregation {unknown}.", "supported": list(_AGG_FUNCS)}
            )

        df, info = await _load(self._cache, self.valves, file_id, __files__, __metadata__, __user__, sheet)
        if df is None:
            return json.dumps(info)

        prov = {
            "operation": operation,
            "file": os.path.basename(info["path"]),
            "active_sheet": info["sheet"],
            "rows_total": int(len(df)),
        }
        rows = df
        if filters:
            rows, err = _apply_filters(df, filters, params, info["decimal"])
            if err:
                return json.dumps(err, default=str)
        prov["filters_applied"] = filters or None
        prov["rows_after_filter"] = int(len(rows))

        run = _OPERATIONS.get(operation) if isinstance(operation, str) else None
        if run is None:
            return json.dumps(
                {"status": "error", "reason": f"Unknown operation '{operation}'.", "supported": list(_OPERATIONS)}
            )
        try:
            result = run(
                _Analysis(self.valves, self._cache, df, rows, info, prov),
                value_column=value_column,
                group_by=group_by,
                columns=columns,
                agg=agg,
                aggs=aggs,
                n=n,
                date_column=date_column,
                freq=freq,
                method=method,
                threshold=threshold,
                subset=subset,
                pipeline=pipeline,
                params=params,
            )
            return json.dumps(result, default=str)
        except Exception as e:
            return json.dumps({"status": "error", "reason": f"{type(e).__name__}: {e}", "provenance": prov})
