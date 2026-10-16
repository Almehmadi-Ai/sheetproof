"""
title: SheetProof Router
author: Abdulrahman Almehmadi
author_url: https://github.com/Almehmadi-Ai/sheetproof
version: 1.2.0
license: MIT
required_open_webui_version: 0.5.0
description: When an Excel/CSV file is attached, keeps it out of retrieval (so its rows are
  not pasted into the prompt), enables the SheetProof tool with native function calling, and
  instructs the model to take every number from the tool. The outlet checks the figures in
  the final answer against the tool results and logs, or optionally annotates, those that do
  not match. Chats without a spreadsheet are left untouched.
"""

import html
import json
import logging
import re
from typing import Optional

from pydantic import BaseModel, Field

_audit_log = logging.getLogger("open_webui.spreadsheet_audit")

SPREADSHEET_EXTS = (".xlsx", ".xls", ".xlsm", ".csv", ".tsv")
TOOL_ID = "sheetproof"
TOOL_FUNCS = ("inspect_spreadsheet", "analyze_spreadsheet")

CONTRACT = (
    "A spreadsheet file is attached, but its contents are NOT in your context. "
    "You can only analyze it through tools.\n"
    "1) Call `inspect_spreadsheet` FIRST to get the file's real columns and roles.\n"
    "2) Call `analyze_spreadsheet` for EVERY number/total/count/average/group/pivot/"
    "ranking/anomaly/trend/statistic.\n"
    "Hard rules (an automated audit checks your answer against tool results):\n"
    "- Never compute, sum, subtract, average, round, or derive any number yourself. "
    "If you need a combined/subtotal/share/percentage figure, get it from the tool "
    "(top_n and group_aggregate return `summary.returned_subtotal` and "
    "`returned_share_of_total_pct`; anomalies returns `flagged_sum` and "
    "`flagged_share_of_total_pct`).\n"
    '- Never COUNT rows yourself. For "how many X are Y" use the tool\'s `breakdown` '
    "(e.g. anomalies breakdown by a dimension), never tally rows in your head.\n"
    "- For dates relative to a reference (today / as-of / expiry / age), pass the date "
    'via params (e.g. params={{"today": "2026-06-16"}}) and compute with '
    "datediff(`Date Column`, @today) in a transform 'derive' step. Never work out day "
    "counts, ages, or date differences yourself.\n"
    "- If a tool step errors or returns null/empty (e.g. 'unknown parameter'), FIX the "
    "call and re-run the tool (add the missing param, correct the column name) — do NOT "
    "fall back to computing the value yourself.\n"
    "- Never state a relationship between cells/rows (e.g. 'same record', 'these are "
    "duplicates') unless a tool result explicitly shows it.\n"
    "- If you show example/highlight rows, do NOT generalize them to the whole set. "
    "Set-level statements must come from a tool breakdown over the full set.\n"
    "- Every figure in your answer must appear in a tool result; mention the rows it "
    "was computed on when relevant.\n"
    "- If a tool returns needs_clarification, ask the user which column to use.\n"
    "- If something cannot be computed from the file, say so instead of inventing it.\n"
    "- For ANY per-row / per-entity listing or table, reproduce the cells EXACTLY from the "
    "tool's row result or its `table_markdown`. Never type, infer, re-rank, or 'correct' a "
    "RECORDED value (rating, status, yes/no, category, name, note, date) — copy each "
    "recorded value verbatim from the tool result for that exact row, even if it seems "
    "inconsistent with the KPIs. To build such a table, call analyze_spreadsheet with "
    "operation 'transform' and a 'select' of the needed columns, and present the returned "
    "table_markdown as-is. You stay free for all overview, distributions, rankings, "
    "recommendations, and narrative analysis.\n"
    "Then answer the user's actual question in whatever natural form best fits it "
    "(summary, comparison, ranking, insights, prose, table). Do NOT use a fixed "
    "template. Available spreadsheet file_id(s): {ids}"
)

# --------------------------------------------------------------------------- #
# Output audit: numbers
# --------------------------------------------------------------------------- #
_NUM_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")
_STRUCTURAL = (
    "step",
    "phase",
    "tier",
    "top ",
    "part",
    "stage",
    "section",
    "table",
    "figure",
    "rank",
    "no.",
    "no ",
    "#",
    "q1",
    "q2",
    "q3",
    "q4",
    "row",
    "rows",
    "item",
    "line",
    "record",
    "page",
    "col",
)


def _to_float(tok: str):
    t = tok.replace(",", "").rstrip("%").strip()
    try:
        return float(t)
    except ValueError:
        return None


def _collect_numbers(text: str) -> list:
    """All numeric values found in a tool-result string -> the 'verified' corpus."""
    out = []
    for m in _NUM_RE.finditer(text or ""):
        v = _to_float(m.group(0))
        if v is not None:
            out.append(v)
    return out


def _candidate_numbers(text: str, mode: str) -> list:
    """Significant figures the model presents in its answer. Returns (raw, value, is_pct)."""
    text = text or ""
    low = text.lower()
    cands = []
    for m in _NUM_RE.finditer(text):
        tok = m.group(0)
        end = m.end()
        is_pct = text[end : end + 1] == "%" or text[end : end + 2] == " %"
        # a separator or digit right before or after means a fragment of a date, code or
        # longer number (the "-28" of 2024-12-28, "0001" in PO-2024-0001), not a figure
        prev_ch = text[m.start() - 1] if m.start() > 0 else ""
        next_ch = text[end + (1 if is_pct else 0) : end + (2 if is_pct else 1)]
        if prev_ch in "/-" or next_ch in "/-" or prev_ch.isdigit() or next_ch.isdigit():
            continue
        v = _to_float(tok)
        if v is None:
            continue
        significant = ("," in tok) or ("." in tok) or is_pct or abs(v) >= 1000
        if not significant:
            if mode != "strict":
                continue
            if abs(v) < 4:
                continue
            if v == int(v) and 1900 <= v <= 2100:  # year-like
                continue
            ctx = low[max(0, m.start() - 14) : m.start()]
            if any(w in ctx for w in _STRUCTURAL):
                continue
        cands.append((tok + ("%" if is_pct else ""), v, is_pct))
    return cands


def _is_verified(value: float, is_pct: bool, corpus: list) -> bool:
    for v in corpus:
        if abs(value - v) <= 1e-6:
            return True
        if round(value, 2) == round(v, 2):
            return True
        # tolerate the model rounding a tool figure to fewer decimals (14.1 vs 14.11)
        if round(value, 1) == round(v, 1):
            return True
        # sign-insensitive: tool stores days-remaining as negative; the model may
        # phrase the same figure as a positive "days overdue".
        if round(abs(value), 2) == round(abs(v), 2):
            return True
        if (not is_pct) and max(abs(value), abs(v)) >= 1000 and abs(value - v) <= 0.5:
            return True
    return False


# --------------------------------------------------------------------------- #
# Output audit: recorded values. Text cells in the answer's tables must match the tool's
# rows for the same entity.
# --------------------------------------------------------------------------- #
def _tool_call_ids(output) -> set:
    """Call ids of the SheetProof function calls in an assistant message's output blocks."""
    return {
        b.get("call_id") or b.get("id")
        for b in output
        if isinstance(b, dict) and b.get("type") == "function_call" and b.get("name") in TOOL_FUNCS
    }


def _norm_val(s) -> str:
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).strip().lower())


def _harvest_records(output) -> list:
    """Collect per-row dict records the tool actually returned (executed rows)."""
    recs = []
    if not isinstance(output, list):
        return recs
    our = _tool_call_ids(output)
    for b in output:
        if isinstance(b, dict) and b.get("type") == "function_call_output":
            if our and b.get("call_id") not in our:
                continue
            try:
                j = json.loads(_block_text(b.get("output")))
            except (TypeError, ValueError):  # not a JSON tool result
                continue
            res = j.get("result") if isinstance(j, dict) else None
            if isinstance(res, list):
                recs.extend([r for r in res if isinstance(r, dict)])
            elif isinstance(res, dict) and isinstance(res.get("rows"), list):
                recs.extend([r for r in res["rows"] if isinstance(r, dict)])
    return recs


def _parse_md_tables(text: str) -> list:
    """Parse markdown tables in the answer -> list of (headers, [row dicts])."""
    out, lines = [], (text or "").split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i].strip()
        if ln.startswith("|") and i + 1 < len(lines) and re.match(r"^\|?[\s:|-]*-[\s:|-]*\|?$", lines[i + 1].strip()):
            headers = [c.strip() for c in ln.strip().strip("|").split("|")]
            rows, j = [], i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                cells = [c.strip() for c in lines[j].strip().strip("|").split("|")]
                if len(cells) == len(headers):
                    rows.append(dict(zip(headers, cells, strict=True)))
                j += 1
            out.append((headers, rows))
            i = j
        else:
            i += 1
    return out


def _audit_categorical(output, answer: str) -> list:
    """Mismatches between text cells in the answer's tables and the tool's rows for the same
    entity, as strings for the log. The entity column is the most distinct text column of the
    returned rows; answer headers are matched to tool columns by word overlap. Numeric cells
    are left to the number audit."""
    records = _harvest_records(output)
    if not records:
        return []
    keys = []
    for r in records:
        for k in r.keys():
            if k not in keys:
                keys.append(k)
    str_cols = [c for c in keys if sum(1 for r in records if isinstance(r.get(c), str)) >= max(2, len(records) // 2)]
    if not str_cols:
        return []

    def distinct(col):
        return len({_norm_val(r.get(col)) for r in records if r.get(col) not in (None, "")})

    entity_col = max(str_cols, key=distinct)
    if distinct(entity_col) < max(2, int(0.6 * len(records))):
        return []  # no clear row key -> don't risk false joins
    entmap = {}
    for r in records:
        ev = _norm_val(r.get(entity_col))
        if ev:
            entmap.setdefault(ev, r)

    flags, seen = [], set()
    for headers, rows in _parse_md_tables(answer):
        hdr_map = {}
        for h in headers:
            ht = set(re.findall(r"[^\W_]+", h.casefold()))
            best, bs = None, 0.0
            for tc in keys:
                tt = set(re.findall(r"[^\W_]+", tc.casefold()))
                if ht and tt:
                    sc = len(ht & tt) / len(ht | tt)
                    if sc > bs:
                        best, bs = tc, sc
            if bs >= 0.5:
                hdr_map[h] = best
        for row in rows:
            rec = next((entmap[_norm_val(v)] for v in row.values() if _norm_val(v) in entmap), None)
            if rec is None:
                continue
            for h, v in row.items():
                tc = hdr_map.get(h)
                if not tc or tc == entity_col or rec.get(tc) is None:
                    continue
                nv, ntv = _norm_val(v), _norm_val(rec.get(tc))
                nv_clean = nv.strip("*_`~ ").strip()
                # skip when either side is empty / null / N-A (missing data is not a mismatch)
                if (
                    not nv
                    or not ntv
                    or ntv in ("nan", "none", "null", "n/a", "na")
                    or nv_clean in ("", "n/a", "na", "-", "—", "none", "null", "no data")
                ):
                    continue
                if re.fullmatch(r"[-+]?[\d,]*\.?\d+\s*%?", nv) or re.fullmatch(r"[-+]?[\d,]*\.?\d+\s*%?", ntv):
                    continue  # numeric -> number audit handles it
                if nv != ntv and nv not in ntv and ntv not in nv:
                    key = (_norm_val(rec.get(entity_col)), tc, nv)
                    if key in seen:
                        continue
                    seen.add(key)
                    flags.append(f"{rec.get(entity_col)} · {h}='{v}' (file '{rec.get(tc)}')")
    return flags


def _block_text(x) -> str:
    """Flatten an output-block text payload (str | list | {text|content} dict)."""
    if x is None:
        return ""
    if isinstance(x, str):
        return x
    if isinstance(x, list):
        return " ".join(_block_text(e) for e in x)
    if isinstance(x, dict):
        return (_block_text(x.get("text")) + " " + _block_text(x.get("content"))).strip()
    return str(x)


def _is_sheet(f: dict) -> bool:
    name = (f.get("name") or (f.get("file") or {}).get("filename") or "").lower()
    return name.endswith(SPREADSHEET_EXTS)


class Filter:
    class Valves(BaseModel):
        enabled: bool = Field(True, description="Master switch for the router.")
        bypass_rag: bool = Field(True, description="Remove spreadsheet files from the RAG path.")
        audit_enabled: bool = Field(True, description="Audit the final answer's numbers against tool results.")
        audit_mode: str = Field(
            "strict",
            description="'strict' (default) checks decimals, percentages, numbers of 1,000 or more, "
            "and integer counts of 4 or more (skipping years and structural numbers such as "
            "'step 4'). 'monetary' skips the integer counts: fewer flags, but a miscounted "
            "tally goes unnoticed.",
        )
        audit_action: str = Field(
            "log",
            description="What to do with unverified figures. 'log' (default) writes them to the "
            "server log and leaves the answer untouched. 'annotate' appends a warning to the "
            "message; some Open WebUI versions then re-render the edited message as raw text. "
            "'off' disables the audit.",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def inlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __metadata__: Optional[dict] = None,
        __request__=None,
    ) -> dict:
        if not self.valves.enabled:
            return body

        files = body.get("files") or []
        sheets = [f for f in files if _is_sheet(f)]
        if not sheets:
            return body  # no spreadsheet -> leave everything untouched

        ids = [fid for fid in (f.get("id") or (f.get("file") or {}).get("id") for f in sheets) if fid]

        if self.valves.bypass_rag:  # other attachments still go through retrieval
            body["files"] = [f for f in files if not _is_sheet(f)]

        # where the tool looks for the attached ids (it still checks access to each one)
        body.setdefault("metadata", {})["analyst_file_ids"] = ids
        if isinstance(__metadata__, dict):
            __metadata__["analyst_file_ids"] = ids

        tool_ids = body.get("tool_ids") or []
        if TOOL_ID not in tool_ids:
            tool_ids.append(TOOL_ID)
        body["tool_ids"] = tool_ids
        params = body.setdefault("params", {})
        params["function_calling"] = "native"

        contract = CONTRACT.format(ids=", ".join(ids) if ids else "(attached)")
        messages = body.get("messages") or []
        messages.insert(0, {"role": "system", "content": contract})
        body["messages"] = messages

        return body

    async def outlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __metadata__: Optional[dict] = None,
        **kwargs,
    ) -> dict:
        """Flag figures in the final answer that do not appear in any SheetProof tool result.
        Only chats that called the tool are audited; by default findings are only logged."""
        action = (self.valves.audit_action or "log").lower()
        if not (self.valves.enabled and self.valves.audit_enabled) or action == "off":
            return body
        try:
            messages = body.get("messages") or []
            asst = next((m for m in reversed(messages) if m.get("role") == "assistant"), None)
            if not asst:
                return body

            content = asst.get("content") or ""
            output = asst.get("output")

            # The verified corpus holds numbers from tool results only, never from reasoning,
            # tool arguments or the answer. Depending on the Open WebUI version the results are
            # in message.output (function_call_output blocks), message.sources (tool
            # citations) or <details type="tool_calls"> blocks in the content.
            corpus, had_tool = [], False
            if isinstance(output, list):
                our_calls = _tool_call_ids(output)
                for b in output:
                    if isinstance(b, dict) and b.get("type") == "function_call_output":
                        if our_calls and b.get("call_id") not in our_calls:
                            continue  # another tool's output
                        had_tool = True
                        corpus.extend(_collect_numbers(_block_text(b.get("output"))))
            for src in asst.get("sources") or []:
                sinfo = src.get("source") or {}
                if TOOL_ID in str(sinfo.get("name") or sinfo.get("id") or "") or sinfo.get("type") == "tool":
                    had_tool = True
                    for doc in src.get("document") or []:
                        corpus.extend(_collect_numbers(str(doc)))
            for blk in re.findall(r'<details type="tool_calls".*?</details>', content, re.S):
                if any(fn in blk for fn in TOOL_FUNCS):
                    had_tool = True
                    res = re.sub(r"<[^>]+>", " ", blk.split("</summary>", 1)[-1])
                    corpus.extend(_collect_numbers(html.unescape(res)))

            if not had_tool or not corpus:
                return body

            # The visible answer: the 'message' output blocks, else the content without its
            # <details> (reasoning and tool call) blocks.
            if isinstance(output, list) and any(isinstance(b, dict) and b.get("type") == "message" for b in output):
                answer = " \n".join(
                    _block_text(b.get("content")) for b in output if isinstance(b, dict) and b.get("type") == "message"
                )
            else:
                answer = re.sub(r"<details.*?</details>", " ", content, flags=re.S)

            flagged, seen = [], set()
            for raw, v, is_pct in _candidate_numbers(answer, self.valves.audit_mode):
                if _is_verified(v, is_pct, corpus):
                    continue
                key = round(v, 4)
                if key in seen:
                    continue
                seen.add(key)
                flagged.append(raw)

            if action == "annotate":
                if flagged:
                    note = (
                        "\n\n> ⚠️ **Unverified figures detected.** These numbers do not match "
                        "any tool result and may have been derived or estimated by the model "
                        "rather than computed from the file — treat them with caution and "
                        "re-run the relevant analysis to confirm: " + "  |  ".join(flagged[:25]) + "."
                    )
                    if note.strip() not in content:
                        asst["content"] = content + note
                        body["messages"] = messages
            else:
                if flagged:
                    _audit_log.warning(
                        "spreadsheet audit: %d unverified figure(s) in answer: %s",
                        len(flagged),
                        "  |  ".join(flagged[:25]),
                    )
                cat_flags = _audit_categorical(output, answer)
                if cat_flags:
                    _audit_log.warning(
                        "spreadsheet audit: %d recorded-value mismatch(es) vs file rows: %s",
                        len(cat_flags),
                        " ; ".join(cat_flags[:25]),
                    )
        except Exception:
            return body
        return body
