# Changelog

All notable changes to this project are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] - 2026-10-16

Security release. Every earlier version is affected by the vulnerabilities below; update **both**
plugins.

### Security
- **Arbitrary code execution through `filters` (critical).** `analyze_spreadsheet` passed the
  model-supplied `filters` string to `pandas.DataFrame.query`, which resolves `@name` references
  against the plugin's module globals and allows method calls. A filter — which a model can be led to
  write through prompt injection — could therefore run operating-system commands, read files the Open
  WebUI process can read, or write files, contrary to the documented "no arbitrary code execution"
  property. Filters are now evaluated by SheetProof's own AST allow-list expression language (the one
  `transform` steps use) and never reach `query` or `eval`. Affects 1.0.0 and 1.1.0.
- **Cross-user file access (high).** When the owner-scoped file lookup found nothing, the tool retried
  with an unscoped lookup by id, and then searched the upload directory for `<file_id>_*`, so
  `file_id="*"` matched any upload. File ids attached to a message were trusted as well, although Open
  WebUI copies them from the client's request. Any signed-in user could analyze another user's
  spreadsheet. Every file id must now be a well-formed Open WebUI id and pass Open WebUI's own read
  rule for files (owner, admin, or a read grant through its access control); there is no unscoped or
  filesystem fallback. Affects 1.0.0 and 1.1.0.
- **Internal methods callable by the model on Open WebUI 0.5 and 0.6 (high).** Those releases offer
  the model every method of a tool's `Tools` class whose name does not start with two underscores,
  so the tool's internal helpers could be called with model-chosen arguments, among them `_read`,
  which opened a spreadsheet at any server path without an access check. `Tools` now defines only
  `inspect_spreadsheet` and `analyze_spreadsheet`. Affects 1.0.0 and 1.1.0.
- Hardening against resource exhaustion and unchecked dispatch: `**` with a non-literal exponent
  (`9**9**9` hung the worker), unbounded text repetition, model-chosen aggregation names passed to
  `getattr` or pandas' method dispatch, arbitrary resample frequencies, unbounded pivots, and ZIP bombs.
  Size limits are now enforced before and while a file is parsed instead of after.

### Changed
- `filters` uses SheetProof's expression language ([docs/usage.md](docs/usage.md#the-expression-language))
  instead of pandas query syntax. The usual forms keep working (`Region == 'North' and Units > 20`,
  backtick column names, `in` lists, `.isna()`, `.between()`, `.str.contains()`); regular expressions,
  `.dt` accessors, `is`, and `@` references to anything but `params` are rejected with an explanation.
  Comparisons are typed (`Revenue > 500` works on currency text), and `!=` keeps blank cells, as in
  pandas.
- An explicit `file_id` is authoritative: a malformed, unknown or unreadable id is an error, and the
  tool no longer falls back to another attached file. Admins can analyze any file, as Open WebUI's file
  API allows them to download any file.
- Unknown column names, ambiguous fuzzy matches and non-numeric measure columns return
  `needs_clarification` (with `columns_available`) instead of a best guess. Unknown `agg`, `aggs`,
  `freq` and anomaly `method` values are errors instead of silently becoming `sum` or `iqr`.
- `top_n` ranks by `agg` (it always ranked by sum); `time_series` and `compare` accept every supported
  `agg`.
- A cell counts as a number only if the whole cell is one: `SKU-100`, `P0` and `12-34` are no longer
  read as numbers.
- Rows with a blank group value form a `(blank)` group in `group_aggregate`, `top_n`, `pivot` and
  `compare`, so the groups add up to the column total; the anomaly breakdown uses the same label
  (previously `(null)`).
- `max_return_rows` caps every operation's rows, groups or periods (it applied only to `anomalies` and
  `transform`), flagging `truncated`.
- CSV files are read with pandas' C engine after delimiter and header detection. Read errors are
  returned as tool errors instead of exceptions.
- `requirements.txt` requires pandas 2.2 (the resample aliases in use need it) and adds xlrd.

### Added
- Valves: `decimal_separator`, `max_file_mb`, `max_uncompressed_mb`, `cache_max_entries`,
  `cache_max_mb`.
- `provenance.number_format` and `number_format` in `inspect_spreadsheet`: the decimal separator used
  for numbers stored as text and what decided it.
- A bounded LRU cache of parsed files, their schema and parsed measure columns, so follow-up questions
  about the same file do not parse it again. Parsing runs in a worker thread instead of on the event
  loop.
- `xlrd` in the tool's `requirements:` header, so legacy `.xls` files load wherever the tool runs.
- CI: ruff lint and format checks; tests on Python 3.11 and 3.12 and against the dependency versions
  Open WebUI 0.12.2 and 0.5 ship; an end-to-end test of router and tool.

### Fixed
- European decimals: `1.234,56` was read as 1.23456 and `1,5` as 15 (a European column summed to 6.73
  instead of 6,734.81). The decimal separator is now inferred per column, apostrophe, space and Indian
  grouping and Arabic-Indic digits are supported, and the choice is reported.
- Column names in Arabic and other non-Latin scripts all normalized to an empty string, so a missing
  column silently matched a different one (asking for الربح returned الإيرادات).
- `group_aggregate` left rows with a blank group value out of `grand_total` and the shares.
- A title row above a CSV header produced columns such as `['Monthly', 'Sales']`.
- `max_cells` was only checked after the whole file had been loaded.
- With `filters`, a row count (`aggregate` with `agg: "count"` and no column) and the `overview` row
  count ignored the filter and reported every row of the file; `rows_processed` did the same for
  several operations.
- The pivot dimensions, `date_column`, `describe` columns and duplicate keys are resolved like other
  column arguments instead of raising `KeyError` or being dropped silently.
- The router's categorical audit skipped answer tables with non-Latin headers.

### Removed
- The upload-directory (glob) fallback for locating files and the hard-coded upload path.
- The empty `assets/screenshots/` folder; the README shows a real tool result instead.

## [1.1.0] - 2026-07-18

### Changed
- **Renamed the project to SheetProof.** The tool is now `sheetproof.py` (tool id `sheetproof`)
  and the companion filter is `sheetproof_router.py`. Analysis behaviour is unchanged, and the
  two tool functions — `inspect_spreadsheet` and `analyze_spreadsheet` — keep their names and
  parameters.
  - *Migrating from 1.0.0:* re-import both files and set the tool id to `sheetproof`. If you
    customized the filter's `TOOL_ID`, make it match your installed tool id.

### Added
- GitHub Actions CI: the test suite on Python 3.11 and 3.12, plus a plugin import check.
- Root `SECURITY.md` security policy, an issue template, and a pull-request template.

## [1.0.0] - 2026-07-18

First public release.

### Added
- **SheetProof tool** (`sheetproof.py`) — a deterministic Excel/CSV
  analysis engine exposing `inspect_spreadsheet` (schema + column roles) and
  `analyze_spreadsheet`, which covers: `overview`, `aggregate`, `group_aggregate`,
  `top_n`, `pivot`, `value_counts`, `describe`, `time_series`, `anomalies`,
  `duplicates`, `missing`, `compare`, and a safe composable `transform` pipeline.
- **SheetProof Router filter** (`sheetproof_router.py`) — automatic activation on
  spreadsheet upload, retrieval bypass so file contents never enter the prompt,
  native function-calling for the turn, a verified-numbers contract, and an output
  audit that flags any figure not backed by a tool result.
- Schema-first column classification, locale-tolerant numeric parsing, header-row
  detection, and full provenance on every result.
- Documentation suite (installation, usage, architecture, security, examples),
  generic sample data, a deterministic sample generator, and tests.
