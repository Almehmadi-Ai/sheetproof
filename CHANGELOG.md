# Changelog

All notable changes to this project are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-07-18

### Changed
- **Renamed the project to SheetProof.** The tool is now `sheetproof.py` (tool id `sheetproof`)
  and the companion filter is `sheetproof_router.py`. Analysis behaviour is unchanged, and the
  two tool functions — `inspect_spreadsheet` and `analyze_spreadsheet` — keep their names and
  parameters.
  - *Migrating from 1.0.0:* re‑import both files and set the tool id to `sheetproof`. If you
    customized the filter's `TOOL_ID`, make it match your installed tool id.

### Added
- GitHub Actions CI: the test suite on Python 3.11 and 3.12, plus a plugin import check.
- Root `SECURITY.md` security policy, an issue template, and a pull‑request template.

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
