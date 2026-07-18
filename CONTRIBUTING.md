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
pip install -r requirements.txt
pip install pytest
python examples/generate_samples.py    # writes examples/sample_sales.{csv,xlsx}
pytest -q
```

The engine is plain pandas and imports cleanly outside Open WebUI, so the tests run the
deterministic core directly.

## Guidelines

- **Keep it domain-neutral.** No hard-coded column names, business vocabulary, or
  assumptions about a particular dataset. Every operation must work on any tabular file.
- **Determinism first.** All numbers come from executed pandas operations and are returned
  with provenance. The language model must never be relied on to compute a value.
- **Safety.** The transform grammar is an AST allow-list, never `eval`/`exec`. Do not add
  attribute access, imports, or non-whitelisted calls to the expression evaluator.
- **Style.** Match the existing code; keep functions small and readable; prefer clear names.

## Pull requests

1. Fork and create a feature branch.
2. Add or update tests for your change.
3. Ensure `pytest` passes and the tool still imports cleanly.
4. Open a PR with a clear description of the change and its motivation.

By contributing, you agree that your contributions are licensed under the project's
[MIT License](LICENSE).
