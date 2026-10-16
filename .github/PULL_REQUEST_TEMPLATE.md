## What does this change?

<!-- A short description of the change. -->

## Why?

<!-- The motivation, and the issue it fixes if applicable. -->

## Checklist

- [ ] **Domain-neutral** — no hard-coded column names, business vocabulary, or dataset assumptions.
- [ ] **Deterministic** — results come from executed pandas operations and carry provenance.
- [ ] **Safe** — no new syntax added to the expression allow-list (attribute access, imports, and
      non-whitelisted calls remain rejected). If the grammar changed, explain why below.
- [ ] Tests added or updated; `ruff check .`, `ruff format --check .` and `pytest -q` pass.
- [ ] Documentation updated if behaviour, operations, or parameters changed.
