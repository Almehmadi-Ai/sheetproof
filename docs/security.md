# Security considerations

SheetProof is designed to be safe to run on real user uploads in a multi-user Open WebUI
instance. This document describes its security properties and the assumptions behind them.

**Threat model.** Every argument of `inspect_spreadsheet` and `analyze_spreadsheet` is chosen by the
language model, and a model can be steered by prompt injection (for example through text in a web
page or a document in the same chat). The list of attached files is copied from the chat request.
The tool therefore treats all of these as untrusted input from the requesting user.

## No arbitrary code execution

The `filters` parameter and the `transform` steps (`filter`, `derive`) are evaluated by one
**restricted expression language**, never by Python `eval`/`exec` and never by pandas'
`DataFrame.query`, `DataFrame.eval` or `pandas.eval` (whose `@name` lookups and method calls reach the
host process):

- Expressions are parsed with `ast.parse(..., mode="eval")` and every node is checked against an
  **allow-list** before evaluation: literals, column references, comparisons, `in` lists, boolean
  logic, arithmetic, calls and keyword arguments.
- Calls are permitted **only** to a fixed set of side-effect-free functions (`where`, `coalesce`,
  `datediff`, `num`, `round`, `contains`, date parts, …) and to a fixed set of column methods
  (`.isna()`, `.between()`, `.isin()`, `.str.contains()`, …). Each is implemented by SheetProof and
  dispatched through an explicit table — model-supplied names are never passed to `getattr`.
- **Other attribute access, indexing, imports, dunder names, comprehensions, lambdas, f-strings and
  any other syntax are rejected** before evaluation. `@name` refers only to the `params` the call
  supplied, never to Python variables.
- Evaluation is bounded: at most 2,000 characters, 400 syntax nodes and a nesting depth of 64 per
  expression; `**` needs a literal exponent of magnitude ≤ 100 and is computed in floating point;
  text is never repeated, and joined text is capped at 10,000 characters per cell.

Aggregation names (`sum`, `mean`, …), resample frequencies and anomaly methods are checked against
allow-lists as well; unknown values are errors.

There is therefore no path from an argument to the filesystem, the network, the Python runtime, or
Open WebUI internals — an expression can only compute over the loaded dataframe.

## File access is access-checked

The tool can be handed a file id in three ways: the model's `file_id` argument, the files attached to
the message (`__files__` / `__metadata__["files"]`), and the id stash written by the router. Open WebUI
copies the attached-file list from the chat request without re-checking it, so **none of these sources
is trusted on its own**:

- An id must look like an Open WebUI file id (a UUID). Wildcards, paths and anything else are rejected
  before any lookup.
- The file record is loaded with `Files.get_file_by_id` and must pass the same read rule Open WebUI's
  own file API applies (`routers/files.py`): the requesting user **owns** the file, is an **admin**, or
  Open WebUI's `has_access_to_file(..., "read", user)` grants access (a knowledge base, channel, shared
  chat, note or model the file is attached to). If that check cannot run, access is denied.
- Only then is the record's path used, via Open WebUI's storage provider. There is no unscoped lookup
  and no filesystem fallback, and an explicit `file_id` is never swapped for a different file.
- "Not found" and "not allowed" return the same error, so the tool cannot be used to probe for ids.

One user therefore cannot analyze another user's file unless Open WebUI already lets them read it.

Parsed files are cached in memory between calls. The cache is only consulted **after** the access check
above, and an entry is keyed by the authorized file id together with the file's real path,
modification time, size and every parsing option, so it cannot be served to a caller who may not read
that file, and it is not reused once the file changes. Entries are never modified by an operation.

## Data minimization

With the router enabled, spreadsheet files are **removed from the retrieval path**, so their contents
are not inserted into the prompt as retrieved context. The model receives the file *id* and works with
the data through tool calls. Most operations return aggregates; operations that list rows (`anomalies`,
`transform`) return at most `max_return_rows` rows, and only the columns the call selects.

## Bounded resource use

Limits are enforced **before or while** a file is parsed, never after it is in memory:

- `max_file_mb` (default 100) — the upload's size on disk is checked before any parser runs.
- `max_uncompressed_mb` (default 500) — `.xlsx`/`.xlsm` files are ZIP archives. Their central directory is
  inspected first: a workbook whose parts would expand beyond this size, or that contains a part larger
  than 10 MB compressed more than 100:1 (a zip bomb), is refused unopened. Python's `zipfile` never
  inflates a part beyond its declared size, so the declared sizes are binding.
- `max_cells` (default 5,000,000) — the header region is read first; the table is then parsed with a row
  budget of `max_cells / columns` (using the sheet's declared width when it is wider), and a file that
  exceeds it is refused after reading one row past the budget.
- Pivots (operation and transform step) are size-checked before they are built; a transform pipeline
  has at most 50 steps; expressions have the budgets listed above.
- `max_return_rows` (default 1,000) caps the rows, groups and periods any operation returns; larger
  results are cut and flagged `truncated: true`, while totals and summaries still cover every row.
- Parsing runs in a worker thread, so a large file does not block other users' requests, and the
  parsed-file cache is bounded by `cache_max_entries` and `cache_max_mb`.

These prevent a single request from exhausting memory or CPU, or producing unbounded output.

## Integrity of results (anti-hallucination)

- The injected contract forbids the model from computing, counting, or deriving numbers itself.
- The output audit builds its "verified" set of numbers **only** from executed tool results — never
  from the model's own narrative or from tool *arguments* — so the model cannot launder a guessed
  number into the verified set.
- Unverified figures are, by default, **logged server-side** and the visible answer is left untouched;
  annotation of the message is opt-in.
- The engine refuses to guess: unknown or ambiguous column names, non-numeric measure columns and
  unknown parameters return `needs_clarification` or an error instead of a best guess.

## Assumptions & residual risk

- **Trust in Open WebUI's auth.** File access relies on Open WebUI correctly identifying the user and on
  its access-control rules. Admins can analyze any file, exactly as they can download any file.
- **Prompt injection through data.** Cell values the tool returns (for example rows from `transform`)
  reach the model and could contain instructions. The tool treats every argument as untrusted, so such
  text cannot make it run code or read files the user could not read anyway.
- **Model tool-calling.** Integrity guarantees apply to numbers the model presents *from tool
  results*. A model that ignores the contract and the tool entirely (a weak tool-caller) should not
  be used for numeric work; the audit will flag such answers but cannot rewrite them by default. The
  audit matches figures heuristically (rounding tolerance, sign), so it can miss or over-flag.
- **Spreadsheet parsing.** Files are parsed with pandas, openpyxl and xlrd (`.xls`). The size checks
  above bound the work, but parser bugs are outside this tool's control: keep those dependencies
  patched.

## Fixed vulnerabilities

Versions 1.1.0 and earlier allowed remote code execution through `filters`, cross-user file access,
and, on Open WebUI 0.5 and 0.6, calls by the model to the tool's internal methods; all are fixed in
1.2.0. See [`CHANGELOG.md`](../CHANGELOG.md).

## Reporting a vulnerability

Report vulnerabilities privately through the repository's **Security** tab (**Report a
vulnerability**), with a minimal reproduction using synthetic data. Do not include private data in
reports, and do not open a public issue for a vulnerability. See [`SECURITY.md`](../SECURITY.md).
