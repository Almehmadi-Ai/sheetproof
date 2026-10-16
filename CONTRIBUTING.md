# Contributing

Thanks for your interest in improving SheetProof. Contributions of all kinds —
bug reports, documentation, and code — are welcome.

## Reporting bugs

Open an issue and include:

- What you asked / the operation and parameters used.
- The shape of your data (columns and roles, not the confidential values).
- What you expected versus what happened, and any error text returned by the tool.

Please do **not** attach files that contain private data. A small, synthetic reproduction
(see `examples/generate_samples.py`) is ideal.

## Development setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate   |   Unix: source .venv/bin/activate
pip install -r requirements-dev.txt     # runtime deps + pinned pytest and ruff
python examples/generate_samples.py    # writes examples/sample_sales.{csv,xlsx}
ruff check . && ruff format --check .  # exactly what CI runs
pytest -q
```

The engine is plain pandas and imports cleanly outside Open WebUI. The tests load both plugins
the way Open WebUI 0.12.2 does and replace the parts of `open_webui` they use (file records,
access control, storage) with small stubs of the same shape, so they need no running server.
CI runs them on Python 3.11 and 3.12, against the latest dependencies and against the exact
versions Open WebUI 0.12.2 and 0.5 ship.

## Guidelines

- **Keep it domain-neutral.** No hard-coded column names, business vocabulary, or
  assumptions about a particular dataset. Every operation must work on any tabular file.
- **Determinism first.** All numbers come from executed pandas operations and are returned
  with provenance. The language model must never be relied on to compute a value.
- **Safety.** Treat every tool argument as untrusted. The expression grammar (used by
  `filters` and the transform steps) is an AST allow-list, never `eval`/`exec`/`DataFrame.query`;
  do not add attribute access, imports, or non-whitelisted calls, and dispatch any new function
  or method through an explicit table, never `getattr` on a model-supplied name. Every file id
  must keep passing the access check before it is read or looked up in the cache.
- **One file per plugin.** Open WebUI installs a tool or function by pasting a single file, so
  `sheetproof.py` and `sheetproof_router.py` stay self-contained: standard library, the packages in
  the `requirements:` header, and `open_webui` only. Put helpers at module level, not on `Tools`:
  older Open WebUI releases offer every method of `Tools` to the model.
- **Style.** Match the existing code; keep functions small and readable; prefer clear names.

## Pull requests

1. Fork and create a feature branch.
2. Add or update tests for your change.
3. Ensure `ruff check .`, `ruff format --check .` and `pytest` pass.
4. Open a PR with a clear description of the change and its motivation.

By contributing, you agree that your contributions are licensed under the project's
[MIT License](LICENSE).
