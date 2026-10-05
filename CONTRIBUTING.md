# Contributing

Contributions are welcome. Please keep changes focused, well-tested, and consistent with the security contract in `SPEC.md`.

## Guidelines

- Keep changes surgical and accompanied by focused tests.
- Never commit real credentials, tokens, Telegram user IDs, private session files, personal local paths, or live runtime logs.
- Run all quality gates locally before opening a pull request:
  ```bash
  uv lock --check
  uv sync --frozen
  uv run python -m compileall -q src tests
  uv run python -m unittest discover -s tests -v
  bash -n ops/install-release.sh ops/manage.sh ops/verify-unit.sh
  ops/verify-unit.sh
  git diff --check
  ```
- All checks must pass cleanly.
