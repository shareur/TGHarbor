# 002 — Feature overview

[简体中文](../zh-CN/0-foundation/002-feature-overview.md)

This document describes the behavior implemented by the current TGHarbor v0.2
codebase. Code and reproducible checks remain authoritative. Features listed under
[Current limitations](#current-limitations) are intentionally not implemented.

## Product model

TGHarbor is a self-hosted Telegram downloader and shared archive manager operated
through a private Bot conversation. A local persistent queue preserves accepted work
across normal process restarts, and completed files enter a local shared library.

Two Telegram clients have distinct responsibilities:

- The Bot client handles private commands, direct or forwarded media, and notifications.
- A separate authorized user-account session resolves individual Telegram message links.

The phone number does not authenticate the Bot. The user session is authorized once
through a trusted local terminal; unattended startup reuses that saved session and never
asks for a verification code through the Bot.

## Access and authorization

- The Bot handles private conversations only; group and channel messages are ignored.
- Every operation checks the sender's numeric Telegram user ID against the configured
  authorized-user list.
- Jobs, job status, history, and cancellation are requester-scoped. An authorized user
  cannot inspect or cancel another user's job.
- The completed-file catalog is shared by all authorized users. Any authorized user can
  list, rename, archive, or create archive directories.
- Rename, archive, and directory creation record the acting Telegram user ID. The shared
  library is not a multi-tenant isolation boundary.

## User journey

### Submit media

An authorized user can send or forward one message containing a Telegram file to the
Bot, or submit exactly one supported message link:

- Public chat: `https://t.me/<username>/<message-id>`
- Private chat: `https://t.me/c/<internal-chat-id>/<message-id>`

The user account must already have access to a linked message. TGHarbor does not crawl
history or bypass Telegram permissions. A submitted link is routed through the user
client even when Telegram attaches an automatic web-page preview to the Bot message.
Only an actual Telegram file is accepted as direct Bot media.

Message-link retrieval runs through the authenticated Telethon MTProto user session, so
a linked file that the authorized account can already access may still be downloadable
even when Telegram marks it save/forward restricted. This remains ordinary account
access: TGHarbor does not grant new access, bypass permissions, or reach content the
account cannot see.

Submitting the same source again for the same requester reuses the existing job instead
of creating a duplicate.

### Inspect and control jobs

`/status` lists only the requester's pending and running jobs. `/history [page]` lists
that requester's completed, failed, and cancelled jobs. `/status <id>` and `/cancel <id>`
accept a full UUID or an unambiguous short prefix within that requester's scope.

Job output uses a short ID, bounded readable description, original or reserved filename,
human-readable size, state, downloaded bytes, and a percentage when the total size is
known. Failed jobs expose only normalized, bounded errors.

### Organize completed files

Completed downloads enter the shared Inbox. The completion notification offers
persistent Keep, Rename, Archive, and Rename + archive actions. Existing archive
directories are available as buttons, while text input and safe directory creation
remain available fallbacks. Pending interaction state survives restart and expires after
24 hours.

`/files [page]`, `/dirs [page]`, and `/stats` inspect the shared library. `/rename`,
`/archive`, and `/mkdir` provide command equivalents for organization. Operations never
silently overwrite another file. See the
[Bot command reference](003-command-reference.md) for exact syntax and scope.

## Metadata and privacy

TGHarbor stores bounded job and catalog metadata needed to operate the service,
including a short message summary limited to 160 Unicode characters. It does not
intentionally persist complete message bodies or raw Telegram objects. Telegram
sessions, the database, audit history, and downloaded files are sensitive operator data.
See [Privacy and data retention](004-privacy-and-retention.md).

## Persistent job lifecycle

| Status | Meaning |
| --- | --- |
| `pending` | Waiting for an available download slot or retry time; can be cancelled. |
| `running` | Download in progress; can complete, fail, retry, or observe cancellation. |
| `completed` | The file and its catalog entry are available. Terminal. |
| `failed` | A permanent error occurred or the attempt limit was exhausted. Terminal. |
| `cancelled` | Cancellation was requested, or a stopped download discarded its partial file. Terminal. |

Queued and running work survives normal restarts. On startup, eligible interrupted jobs
continue as new attempts, cancellation requests are honored, and jobs that exhausted
their attempts fail. Byte-range resume is not guaranteed: interrupted partial bytes are
discarded and downloaded again. Retryable failures use bounded delay, and Telegram flood
waits are honored when longer.

## Local storage and catalog

The configured download root uses this layout:

```text
downloads/
├── inbox/
├── archive/
└── .partial/
```

- Completed files begin in `inbox/`; `archive/` is managed by authorized users.
- In-progress bytes remain hidden under `.partial/`.
- Names are collision-safe: a conflicting destination receives a short job-ID suffix
  and no existing file is overwritten.
- Paths must stay below the configured download root. Absolute paths, traversal,
  symbolic links, and unexpected filesystem objects are rejected.
- Interrupted completion or organization is recovered when safe or left for operator
  review without deleting user content.

The database catalog is the authoritative index of completed files; the filesystem
and database must be backed up and restored as one consistency unit.

## Configuration and deployment

TGHarbor requires Python 3.15 or newer. Strict YAML configuration defines Telegram
credentials and authorized users, download behavior, database/session paths, and log
level. Supported environment variables can override configured values. Unknown or
invalid values stop startup, and secret fields are excluded from object
representations.

`tgharbor login` authorizes the user session in a trusted terminal. `tgharbor run`
starts recovery, Telegram clients, background download work, and Bot handling. Only one
application instance may use the same state. The supplied container runs as a non-root
user; do not scale it above one replica.

See [Development setup](001-development-setup.md) and the
[operations guide](../3-operations/301-core-downloader-operations.md) for complete
configuration and startup procedures.

## Persistent data and backup scope

Operators must protect and back up:

- The SQLite database and sidecars: jobs, catalog, audit, interactions, and paths.
- The complete download root: Inbox, Archive, partial/recovery state, and user files.
- Telegram sessions and local configuration.

Stop the service for a filesystem-level backup. Keep the database and download root
in the same restore point; mixing pre-upgrade and post-migration state can orphan or
misidentify files. Follow the
[v0.2 upgrade and rollback guide](../3-operations/303-v0-2-upgrade.md).

## Current limitations

The current release intentionally does not provide:

- Channel/chat history crawling, message ranges, or bulk import.
- Telegram permission bypass.
- A Bot-only runtime mode; individual message links require an authorized user session.
- Guaranteed byte-range resume.
- Multiple application replicas or distributed Workers.
- Cloud/object storage; only local filesystem storage is implemented.
- A web UI, REST API, public CLI management API, or scheduler.
- Content search, tags, content-hash deduplication, or automatic retention/deletion.
- Live Telegram, image, migration, or host acceptance without operator-owned
  credentials and infrastructure. Automated tests use fake Telegram clients.

## Related documents

- [Bot command reference](003-command-reference.md)
- [Privacy and data retention](004-privacy-and-retention.md)
- [System overview](../1-architecture/101-system-overview.md)
- [File catalog and interactions](../1-architecture/102-file-catalog-and-interactions.md)
- [v0.2 implementation and acceptance](../2-implementation/202-v0-2-library-workflow.md)
- [Core Downloader operations](../3-operations/301-core-downloader-operations.md)
