# Contributing to TGHarbor

Thank you for improving TGHarbor. Keep changes small, reviewable, and aligned with the
self-hosted Core Downloader scope.

## Before opening a change

1. Search existing issues and documentation.
2. Do not include credentials, Telegram sessions, private links, user content,
   databases, logs, or private deployment identifiers.
3. For behavior changes, define inputs, outputs, failure behavior, authorization, and
   acceptance criteria before implementation.
4. Preserve requester ownership for jobs and shared authorization plus operator audit
   for library operations.

## Development

Use Python 3.15 or newer:

```shell
python3.15 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m pytest -q
```

Update tests and affected English documentation. Mirror public documentation changes
under `docs/zh-CN/` and update navigation. Do not copy code from reference projects.

## Pull requests

- Explain the user-visible behavior and security/data impact.
- List verification actually run and any unverified live acceptance.
- Keep unrelated formatting or refactors out of the change.
- Do not commit, push, deploy, or rotate operator credentials on another person's
  behalf without explicit authorization.

Security reports do not belong in public issues; follow `SECURITY.md`.
