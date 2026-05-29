# Contributing to second-brain-mcp

Thanks for your interest in contributing. This is a small project; please open an issue before starting large changes.

## Setup

```bash
git clone https://github.com/ddmanyes/second-brain-mcp
cd second-brain-mcp
uv sync --dev
uv run playwright install chromium
```

## Running tests

```bash
uv run pytest tests/ -q --tb=short
```

All 115 tests must pass. New code should include tests. Coverage threshold is 70%.

## Code style

- Python 3.11+, type hints on all function signatures
- Follow PEP 8 (enforced by ruff)
- No new comments unless the *why* is non-obvious

## Security rules

Before submitting PRs that touch user-facing input or outbound requests, check:

- File paths from untrusted sources must be validated with `.resolve().is_relative_to(vault)` before access
- HTTP/HTTPS URLs must pass `_is_ssrf_safe()` before any fetch
- Never pass `--yolo` to Gemini CLI subprocesses
- YAML scalar values must use `json.dumps(value.strip())[1:-1]` not manual `.replace()`

## Pull request checklist

- [ ] Tests pass (`uv run pytest`)
- [ ] New behaviour has corresponding tests
- [ ] Security rules above are respected
- [ ] PR description explains *why*, not just *what*

## Reporting bugs

Use the [Bug Report](.github/ISSUE_TEMPLATE/bug_report.md) template — it asks for the minimal information needed to reproduce.
