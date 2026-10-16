# Installation

SheetProof is two Open WebUI plugins: a **Tool** (required) and a **Filter**
(recommended). Each is a single file that you paste into Open WebUI.

## Requirements

- **Open WebUI 0.5.0 or newer.**
- Python packages `pandas` (2.2 or newer), `numpy`, `openpyxl`, and `xlrd` (legacy `.xls` files).
  These are declared in the tool header, and Open WebUI installs missing packages automatically on
  first load; Open WebUI 0.5 and newer already ship all of them.
- A model that supports **tool / function calling** well.

## Manual import

### 1. Install the Tool

1. Go to **Workspace → Tools** and click **+** (Create Tool).
2. Paste the entire contents of [`sheetproof.py`](../sheetproof.py).
3. Set the tool **id** to `sheetproof` and save.

### 2. Install the Filter (recommended)

1. Go to **Workspace → Functions** and click **+** (Create Function).
2. Paste the entire contents of [`sheetproof_router.py`](../sheetproof_router.py) and save.
3. Enable the function **globally** (or per-model). The filter only acts on chats that have a
   spreadsheet attached; every other chat passes through untouched.

> If you set a different tool id, update the filter's `TOOL_ID` constant to match.

## From the Open WebUI Community site

If you found SheetProof on the Open WebUI Community site, its page has **Get** / **Import to WebUI**:
choose your instance URL and confirm. The tool appears under **Workspace → Tools**; import the
**SheetProof Router** filter the same way (it appears under **Workspace → Functions**).

## Enabling the tool for a model

1. Open **Workspace → Models**, edit the model you use for data work.
2. Under **Tools**, enable **SheetProof**.
3. Under advanced parameters, set **Function Calling** to **Native**.
   (The router sets this automatically for spreadsheet chats, but setting it on the model is a
   safe default if you ever use the tool without the router.)

## Verifying the installation

1. Start a new chat, attach [`examples/sample_sales.csv`](../examples/sample_sales.csv).
2. Ask: *"Give me an overview of this file."*
3. You should see the model call `inspect_spreadsheet` / `analyze_spreadsheet` and answer with a
   schema summary and per-measure statistics — with the tool call visible in the message details.

If the model answers **without** calling the tool, its function calling is not set to native, or
the model is a weak tool-caller — switch models or enable native function calling.

## Settings (valves)

Both plugins are configured through their valves (the gear icon next to the tool / function).

**SheetProof (tool)**

| Valve | Default | Meaning |
| --- | --- | --- |
| `max_cells` | 5,000,000 | Refuse tables larger than rows × columns. Enforced while reading: at most `max_cells / columns` rows are parsed. |
| `max_file_mb` | 100 | Refuse uploads larger than this many megabytes before reading them. |
| `max_uncompressed_mb` | 500 | Refuse Excel workbooks whose contents would expand beyond this, or that contain a part compressed more than 100:1. |
| `max_return_rows` | 1,000 | Most rows, groups or periods any operation returns; longer results are cut and flagged `truncated`. |
| `ambiguity_min_gap` | 0.15 | How clearly the best fuzzy match for a column name must beat the runner-up before it is used; otherwise the tool asks. |
| `decimal_separator` | `auto` | Decimal separator for numbers stored as text when a column's values are ambiguous (`1,234`): `auto` follows the rest of the file, then `.`; or set `.` or `,`. |
| `cache_max_entries` | 8 | Parsed spreadsheets kept in memory between calls (0 disables the cache). |
| `cache_max_mb` | 512 | Memory budget of that cache, in megabytes. |

**SheetProof Router (filter)**

| Valve | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Master switch. |
| `bypass_rag` | `true` | Remove spreadsheet files from retrieval, so their rows are not pasted into the prompt. |
| `audit_enabled` | `true` | Audit the final answer's numbers against tool results. |
| `audit_mode` | `strict` | `strict` also checks integer counts ≥ 4 (skipping years and structural numbers); `monetary` only checks decimals, thousands, percentages and large numbers. |
| `audit_action` | `log` | `log` records unverified figures server-side; `annotate` appends a warning to the answer; `off` disables the audit. |

## Updating

Paste the updated file over the existing tool/function (or re-import it from the Community
site). Settings (valves) are preserved. Update **both** plugins together; see the
[changelog](../CHANGELOG.md) for behaviour changes (1.2.0 is a security release).

## Uninstalling

Delete **SheetProof** from **Workspace → Tools** and **SheetProof Router** from
**Workspace → Functions**. No other configuration is changed.
