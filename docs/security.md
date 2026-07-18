# Security considerations

SheetProof is designed to be safe to run on real user uploads in a multi‑user Open WebUI
instance. This document describes its security properties and the assumptions behind them.

## No arbitrary code execution

The `transform` pipeline evaluates expressions through a **restricted grammar**, not Python `eval`
or `exec`:

- Expressions are parsed with `ast.parse(...)` and every node is checked against an **allow‑list**
  (arithmetic, comparison, boolean logic, names, constants, lists/tuples).
- Function calls are permitted **only** to a fixed set of whitelisted, side‑effect‑free functions
  (`where`, `coalesce`, `datediff`, `num`, `round`, `contains`, date parts, etc.).
- **Attribute access, imports, dunder names, comprehensions, lambdas, and any other syntax are
  rejected** before evaluation.

There is therefore no path from an expression to the filesystem, the network, the Python runtime, or
Open WebUI internals — an expression can only compute over the loaded dataframe.

## File access is owner‑scoped

The tool resolves an uploaded file through Open WebUI's file model **for the requesting user**
(`get_file_by_id_and_user_id`). It does not read arbitrary paths supplied by the model; the only
filesystem fallback is a lookup by the same file id within the configured uploads directory. One
user cannot analyze another user's file.

## Data minimization

With the router enabled, spreadsheet files are **removed from the retrieval path**, so their contents
are never embedded into the prompt, never sent to the model as context, and never logged as prompt
text. The model receives only the file *id* and interacts with the data exclusively through tool
calls that return computed results.

## Bounded resource use

- `max_cells` (default 5,000,000) rejects oversized files before loading them into memory.
- `max_return_rows` (default 1,000) caps the number of detail rows any operation returns; larger
  results are summarized and flagged `truncated: true`.

These prevent a single request from exhausting memory or producing unbounded output.

## Integrity of results (anti‑hallucination)

- The injected contract forbids the model from computing, counting, or deriving numbers itself.
- The output audit builds its "verified" set of numbers **only** from executed tool results — never
  from the model's own narrative or from tool *arguments* — so the model cannot launder a guessed
  number into the verified set.
- Unverified figures are, by default, **logged server‑side** and the visible answer is left untouched;
  annotation of the message is opt‑in.

## Assumptions & residual risk

- **Trust in Open WebUI's auth.** File ownership relies on Open WebUI correctly identifying the user.
- **Model tool‑calling.** Integrity guarantees apply to numbers the model presents *from tool
  results*. A model that ignores the contract and the tool entirely (a weak tool‑caller) should not
  be used for numeric work; the audit will flag such answers but cannot rewrite them by default.
- **Spreadsheet parsing.** Files are parsed with pandas/openpyxl. Keep those dependencies patched.

## Reporting a vulnerability

Please open a security‑labeled issue (or contact the author) with a minimal reproduction using
synthetic data. Do not include private data in reports.
