# TGHarbor

[简体中文](docs/zh-CN/README.md)

**A self-hosted, recoverable Telegram downloader and shared archive manager.**

TGHarbor accepts media sent or forwarded to a private Bot and individual Telegram
message links that an independently authorized user account can already access. It
persists download jobs in SQLite, downloads into a safe local Inbox, and lets
authorized users organize a shared library through Bot commands and inline actions.

> Early development: back up state before upgrades. Configuration and internal APIs
> may change before v1.0.

## Features

- **Save/forward-restricted media, within your access:** TGHarbor downloads through the
  operator's authorized Telegram account session over MTProto, so it can save media that
  account can already access, including content the Telegram client marks as
  save/forward restricted. It never bypasses Telegram permissions or retrieves content
  the account cannot access.
- Dual Telegram clients: Bot intake plus user-session message-link downloads.
- Requester-scoped persistent jobs with progress, cancellation, retry, and restart recovery.
- Bounded metadata: 160-character message summaries, filenames, MIME types, and file sizes.
- Shared `inbox/` and `archive/` library with persistent keep, rename, archive, and
  directory-creation actions.
- Collision-safe local storage with path, file-type, and symbolic-link defenses.
- Transactional SQLite v1-to-v2 migration and legacy UUID-directory reconciliation.
- Non-root Python 3.15 container deployment and security-focused CI.

## Usage

![Diagram of two TGHarbor intake paths: a forwardable video file message forwarded to the private TGHarbor Bot, and a message that cannot be forwarded replaced by one copied https://t.me message link sent to the private TGHarbor Bot; both show green checks for request acceptance only, not a completed download or archive](docs/assets/readme-usage.svg)

TGHarbor accepts media in two ways:

1. **Forwardable media:** forward one Telegram message containing a file to the private TGHarbor Bot.
2. **Message cannot be forwarded:** copy that single message's supported `https://t.me/...` link and send only the link to the Bot.

Only configured authorized users can submit. Message links resolve through the operator's already-authorized Telegram user session, so only content that account already accesses is retrievable; TGHarbor does not bypass Telegram permissions.

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
chmod 600 .env
chmod 644 config.yaml
docker compose pull
docker compose run --rm tgharbor login
docker compose up -d
```

`chmod 600 .env` keeps credentials private; `chmod 644 config.yaml` keeps the
host-owned file readable by the container's non-root UID/GID `10001`.

The template pulls the published multi-platform image
[`shareur/tgharbor:0.2.1`](https://hub.docker.com/r/shareur/tgharbor) and does not
build from the local checkout. The image entrypoint is `tgharbor` and its default
command is `run`; the login command above overrides it once to authorize the user
session. The template uses Docker-managed volumes for database/session state and
downloads, and contains no fixed host storage path. Never commit `compose.yaml`,
`config.yaml`, `.env`, Telegram sessions, SQLite databases, logs, or downloaded
content. Verify the resolved image digest and OCI labels before rollout as described
in the operations guide.

## Container image releases

Published images are available on
[Docker Hub](https://hub.docker.com/r/shareur/tgharbor) as `shareur/tgharbor`. The
`0.2.1` release targets `linux/amd64` and `linux/arm64` and publishes the aliases
`0.2.1`, `0.2`, `latest`, and `sha-13c5bc7645b9` for one multi-platform image index:
`sha256:b40996ef4938f8545e5eaee86c91fc57b3d27f9b087353d02a97ae7548c5f560`.
That index identifies source revision
`13c5bc7645b9ba27ea9837be3697fa1388ffb44d` at
`https://github.com/shareur/TGHarbor`.

Tags are mutable aliases. Pin `0.2.1` rather than `latest`, and compare the resolved
`RepoDigest` and the `org.opencontainers.image.version`, `.revision`, and `.source`
labels with this release record before deployment. The release workflow publishes
stable tags only after the exact tag-push `CI` run succeeds and records Buildx
provenance and an SBOM. Local source builds remain available for development and are
not release images.

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
- [Release security](docs/3-operations/302-release-security.md)
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
