# 001 — Development setup

[简体中文](../zh-CN/0-foundation/001-development-setup.md)

Run the following commands from the repository root using Python 3.15 or newer.
CI checks the minimum supported version, Python 3.15.

## Environment

```bash
python3.15 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
pip install -e '.[dev]'
```

## Checks and startup

```bash
python -m ruff check .
python -m pytest -q
tgharbor
```

Use `tgharbor login` once in a trusted interactive terminal to create the user
session, then use `tgharbor run` (or simply `tgharbor`) to start the service.
The service refuses to prompt for a verification code during unattended startup.

TGHarbor is operated through a Bot, but v0.1 deliberately uses two Telegram
clients. The Bot token authenticates the Bot and downloads media sent or forwarded
directly to it. The phone number authorizes a separate user-account session used
only to open submitted single-message links that the account can already access.
The phone number is not the Bot login, and it may remain `null` in `config.yaml` so
the one-time `tgharbor login` command prompts for it locally. The persisted user
session prevents repeated phone/code prompts. Because message-link downloads are a
v0.1 requirement, service startup currently requires this authorized user session.

## Configuration

```bash
cp config.example.yaml config.yaml
```

Relative paths are resolved from the configuration file's directory. Unknown
settings and invalid values stop startup. Keep credentials out of Git and prefer
the following environment variables for secrets:

- `TGHARBOR_TELEGRAM_API_ID`
- `TGHARBOR_TELEGRAM_API_HASH`
- `TGHARBOR_TELEGRAM_BOT_TOKEN`
- `TGHARBOR_TELEGRAM_PHONE`
- `TGHARBOR_TELEGRAM_AUTHORIZED_USERS` (comma-separated numeric user IDs)

Set `TGHARBOR_CONFIG` to use a file other than `./config.yaml`. Download, database,
session, retry, concurrency, timeout, and log settings also have `TGHARBOR_*`
overrides matching the names in `config.example.yaml`.

See [Core Downloader operations](../3-operations/301-core-downloader-operations.md)
for Telegram setup, Bot commands, Docker startup, backup, and recovery procedures.
