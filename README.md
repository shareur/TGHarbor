# TGHarbor

[简体中文](docs/zh-CN/README.md)

**A self-hosted, recoverable Telegram downloader and shared archive manager.**

TGHarbor accepts media sent or forwarded to a private Bot and individual Telegram
message links that an independently authorized user account can already access. It
persists download jobs in SQLite, downloads into a safe local Inbox, and lets
authorized users organize a shared library through Bot commands and inline actions.

> Early development: back up state before upgrades. Configuration and internal APIs
> may change before v1.0.

## Highlights

- Dual Telegram clients: Bot intake plus user-session message-link downloads.
- Requester-scoped jobs with persistent progress, cancellation, retry, and recovery.
- Readable 160-character message summaries, filenames, MIME types, and file sizes.
- Shared `inbox/` and `archive/` library with rename, archive, and directory creation.
- Persistent keep/rename/archive interactions that survive process restarts.
- Collision-safe local storage with path, file-type, and symbolic-link defenses.
- Transactional SQLite v1-to-v2 migration and legacy UUID-directory reconciliation.
- Non-root Python 3.15 container deployment and security-focused CI.

## Quick start

Requirements: Python 3.15+, a Telegram application ID/hash, a BotFather token, and a
Telegram user account for message-link access.

For a local Python process, copy and edit the YAML file, then install and run TGHarbor:

```shell
python3.15 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp config.example.yaml config.yaml
.venv/bin/tgharbor login --config config.yaml
.venv/bin/tgharbor run --config config.yaml
```

For Docker Compose, copy the portable template and credential examples first:

```shell
cp compose.example.yaml compose.yaml
cp config.example.yaml config.yaml
cp .env.example .env
docker compose build --pull
docker compose run --rm tgharbor login
docker compose up -d
```

The template uses Docker-managed volumes for database/session state and downloads;
it contains no fixed host storage path. Never commit `compose.yaml`, `config.yaml`,
`.env`, Telegram sessions, SQLite databases, logs, or downloaded content. Docker
backup, restore, and optional host bind-mount steps are documented in the operations
guide.

## Bot workflow

1. Send or forward media, or submit one supported `https://t.me/...` message link.
2. Use `/status` for active jobs and `/history` for terminal jobs.
3. On completion, choose Keep, Rename, Archive, or Rename + archive.
4. Browse the shared library with `/files`, `/dirs`, and `/stats`.

All authorized users may manage the shared file library; each rename, archive move,
and directory creation records the acting Telegram user ID. Job lookup and
cancellation remain requester-scoped.

## Documentation

- [Documentation index](docs/README.md)
- [Development setup](docs/0-foundation/001-development-setup.md)
- [Feature overview](docs/0-foundation/002-feature-overview.md)
- [Bot command reference](docs/0-foundation/003-command-reference.md)
- [Privacy and retention](docs/0-foundation/004-privacy-and-retention.md)
- [Architecture](docs/1-architecture/102-file-catalog-and-interactions.md)
- [Operations](docs/3-operations/301-core-downloader-operations.md)
- [v0.2 upgrade](docs/3-operations/303-v0-2-upgrade.md)

## Contributing and security

Read [CONTRIBUTING.md](CONTRIBUTING.md) and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
before contributing. Do not put credentials, sessions, private links, user content,
or private deployment details in issues or pull requests. Follow [SECURITY.md](SECURITY.md)
for vulnerability reporting and the release-security checklist before publishing a
fork or release.

## Status

The v0.2 shared-library workflow is implemented and covered by credential-free
automated tests. Live Telegram, container-image, migration-rehearsal, and deployment
acceptance require operator-owned infrastructure and are tracked separately; they
must not be inferred from unit-test results.

## License and acknowledgement

Apache License 2.0. TGHarbor is a clean-room, independent implementation inspired by
Telegram downloader workflows, including `jsavargas/telethon_downloader`; no reference
project code is copied.
