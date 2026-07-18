# Installation

SheetProof is two Open WebUI plugins: a **Tool** (required) and a **Filter**
(recommended). You can install from the Open WebUI Community or paste the files manually.

## Requirements

- **Open WebUI 0.5.0 or newer.**
- Python packages `pandas`, `numpy`, `openpyxl`. These are declared in the tool header, and
  Open WebUI installs missing packages automatically on first load. (Most instances already
  have pandas/numpy.)
- A model that supports **tool / function calling** well.

## Option A — Open WebUI Community

1. Open the tool's Community page (linked from the repository description).
2. Choose **Get** / **Import to WebUI** and select your instance URL.
3. Confirm the import. The tool appears under **Workspace → Tools**.
4. Repeat for the **SheetProof Router** filter, which appears under **Workspace → Functions**.

## Option B — Manual import

### 1. Install the Tool

1. Go to **Workspace → Tools** and click **+** (Create Tool).
2. Paste the entire contents of [`sheetproof.py`](../sheetproof.py).
3. Set the tool **id** to `sheetproof` and save.

### 2. Install the Filter (recommended)

1. Go to **Workspace → Functions** and click **+** (Create Function).
2. Paste the entire contents of [`sheetproof_router.py`](../sheetproof_router.py) and save.
3. Enable the function **globally** (or per‑model). The filter only acts on chats that have a
   spreadsheet attached; every other chat passes through untouched.

> If you set a different tool id, update the filter's `TOOL_ID` constant to match.

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
   schema summary and per‑measure statistics — with the tool call visible in the message details.

If the model answers **without** calling the tool, its function calling is not set to native, or
the model is a weak tool‑caller — switch models or enable native function calling.

## Updating

Re‑import the newer version from the Community page, or paste the updated file over the existing
tool/function. Settings (valves) are preserved.

## Uninstalling

Delete **SheetProof** from **Workspace → Tools** and **SheetProof Router** from
**Workspace → Functions**. No other configuration is changed.
