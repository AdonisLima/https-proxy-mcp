---
description: Run every sensor this repository has — tests, lint, format, types
allowed-tools: Bash(uv run:*)
---

Run all four checks and report the results plainly. Do not stop at the first failure; run all of
them so the report is complete.

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

Expected: `675 passed, 3 skipped`, `All checks passed!`, `76 files already formatted`,
`Success: no issues found in 37 source files`.

The 3 skipped are the live machine tests, which are gated on `HTTPS_PROXY_MCP_E2E=1` and change
the developer's real Mac. They are *supposed* to skip here. Do not set that variable to make the
skips go away — if they need running, use `/live-e2e`.

If the test count differs from 675, say so explicitly rather than reporting "tests pass": a
changed count means tests were added or lost, and both are worth naming.
