# Security Policy

## Supported versions

The latest release on `main` receives security fixes.

## Reporting a vulnerability

Please open a security‑labeled issue with a **minimal reproduction using synthetic data**
(see `examples/generate_samples.py`). Do not include private, production, or personal data in
a report. If the issue is sensitive, open a GitHub security advisory on this repository instead.

## Security properties

SheetProof is designed to run on real user uploads in a multi‑user Open WebUI instance:

- **No arbitrary code execution.** The `transform` expression grammar is parsed to an AST and
  validated against an allow‑list — it is never `eval`/`exec`. Attribute access, imports, dunder
  names, comprehensions and non‑whitelisted calls are rejected before evaluation.
- **Owner‑scoped file access.** Uploaded files are resolved through Open WebUI's file model for the
  requesting user; the tool does not read arbitrary model‑supplied paths.
- **Data minimization.** With the router enabled, spreadsheet contents are removed from the retrieval
  path — raw rows never enter the model prompt.
- **Bounded resources.** `max_cells` rejects oversized files and `max_return_rows` caps returned
  rows, flagging `truncated` rather than silently dropping data.

The full analysis, including assumptions and residual risk, is in
[`docs/security.md`](docs/security.md).
