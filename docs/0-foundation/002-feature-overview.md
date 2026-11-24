# 002 — Feature overview

[简体中文](../zh-CN/0-foundation/002-feature-overview.md)

This document describes the behavior implemented by the current TGHarbor v0.2
codebase. Code and reproducible checks remain authoritative. Features listed under
[Current limitations](#current-limitations) are intentionally not implemented.

## Product model

TGHarbor is a self-hosted Telegram downloader and shared archive manager operated
through a private Bot conversation. A local SQLite queue, asynchronous Workers, and
filesystem storage preserve work across normal process restarts.

Two independent Telegram clients have distinct responsibilities:

| Client | Authentication | Responsibility |
| --- | --- | --- |
| Bot client | BotFather token | Private Bot conversation, commands, direct/forwarded media and albums, notifications, and startup controls |
| User client | One-time phone, verification code, and optional two-step password | Media referenced by individual Telegram message links |

The phone number does not authenticate the Bot. It authorizes the separate user
session once through a trusted local terminal. Unattended startup reuses that saved
session and never asks for a verification code through the Bot.

## Access and authorization

- The Bot handles private conversations only; group and channel messages are ignored.
- Every operation checks the sender's numeric Telegram user ID against
  `telegram.authorized_users`.
- Jobs, job status, history, and cancellation are requester-scoped. An authorized
  user cannot inspect or cancel another user's job.
- The completed-file catalog is shared by all authorized users. Any authorized user
  can list, rename, archive, or create archive directories.
- Rename, archive, and directory-creation audit rows record the acting Telegram user
  ID. The shared library is not a multi-tenant isolation boundary.

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

A Telegram album is accepted once by the album handler and submitted as one opaque
batch. Every media message in the album is queued with the album's exact member
count, while a single message remains a one-member job. The single-message handler
ignores grouped album messages so they are not submitted twice. A message link that
resolves to one album member is not expanded or crawled; it remains one job.

The queue deduplicates a source per requester. Repeating the same source returns the
existing persistent job rather than creating a second job.

### Inspect and control jobs

`/status` lists only the requester's pending and running jobs. `/history [page]`
lists that requester's completed, failed, and cancelled jobs. `/status <id>` and
`/cancel <id>` accept a full UUID or an unambiguous short prefix within that
requester's scope.

Job output uses a short ID, bounded readable description, original or reserved
filename, human-readable size, state, downloaded bytes, and a percentage when the
total size is known. Failed jobs expose only normalized, bounded errors.

### Organize completed files

Completed downloads enter the shared Inbox. The success notification always uses this
field order and English labels: `Download Completed`, `Job` (the short job ID),
`File Name` (the final catalog name), `Download Folder` (a logical
`/download/<relative parent>` path, never a host absolute path), `File Size` in MB with
two decimals, `Start Time` and `End Time` as server-local `HH:MM:SS`, `Download Time`
in seconds, `Download Speed` in KB/s, `User Id`, `Origin Group`, and `Channel ID`.
Missing timestamps, elapsed values,
sizes, or source peers render `N/A`. Time and speed come from the persisted attempt
timestamps and stored catalog size, not from notification wall-clock time.

The same notification offers persistent Keep, Rename, Archive, and Rename + archive
actions. A successfully completed album of more than one member can produce one
persistent batch-complete message with `Move all to new folder`. A durable outbox
redrives eligible notices at startup and periodically while the service runs, so a
failed send or record does not lose the notice, and `sent` is terminal. An
interaction records user intent and callback identity, not proof that Telegram
accepted a message; a failed send or mark keeps and reuses the same interaction.
Delivery is at-least-once, so a crash after the send but before recording `sent` can
duplicate the chat notice while the replay still carries the same callback identity.
The button opens existing archive directories in recent-use order plus New directory.
Directory selection and new-directory creation are explicit confirmation steps, and
the result reports moved, skipped, and failed counts. Directory buttons bind an
opaque digest to the displayed directory and ordering, so a reordered or changed list
rejects the old choice as stale instead of selecting another directory. A result with
failed or not-ready members keeps the interaction and offers a persistent Retry move
action. Batch movement is Inbox-only, per-file, collision safe, requester-authorized,
and replay-idempotent. Ordinary actionable interaction and button state survive
restart and expire after 24 hours without activity. Non-actionable undelivered
`result-pending` and `retry-pending` rows survive that expiry for startup recovery, and
a recovered Retry card that becomes actionable receives a fresh expiry.

Organization is one-card and edit-first. Keep, Rename, Archive, Rename + archive, and
batch Move all edit the completion card in place; directory choice, New directory,
confirmation, working state, result, cancellation, and stale feedback also replace
that same bot message. A concise `Job <short-id> · <file>` or `Album · <count> files`
header stays visible. The confirmation offers `Move here` for an existing directory
or `Create and move` for a new one, with `Back` and `Cancel`. Invalid or slash-prefixed
folder input stays in the waiting step with a relative-path example and never creates a
job. Only a provably uneditable message falls back to exactly one replacement; a
replacement prompt ID is bound with a compare-and-swap against the expected anchor.
Only one text workflow is active per actor through atomic activation, and starting a
new rename or directory workflow leaves page interactions and other completion
buttons intact. A single WORKING claim makes duplicate confirmation callbacks report
processing while one library operation runs. Accepted link/upload messages report
`Download queued · <short-id>`, the source, pending status, and the next `/status`
command; an album reports its file count, initial status, and that Move all appears
after every file completes.

`/files [page]`, `/dirs [page]`, and `/stats` inspect the shared library. `/rename`,
`/archive`, and `/mkdir` provide command equivalents for organization. Operations
never silently overwrite another file. See the
[Bot command reference](003-command-reference.md) and the
[Telegram interaction UX](../1-architecture/103-telegram-interaction-ux.md) for exact
syntax, scope, templates, and routing.

## Metadata and privacy

SQLite stores job source identifiers, requester and operator IDs, state, progress,
paths, original/current filenames, MIME type, size, plus optional opaque batch,
attempt-timing, and original source group/channel metadata. `origin_group_id` is the
original group or channel peer only when Telegram supplies an unambiguous peer;
`channel_id` is set only for an original/source channel, never inferred from the
original group or requester. Private or unavailable metadata stays empty and renders
`N/A`. It also stores a whitespace-normalized Telegram message summary limited to 160
Unicode characters. TGHarbor does not intentionally persist complete message bodies
or raw Telegram objects. Telegram
sessions, the database, audit history, interaction state, and downloaded files are
sensitive operator data. See [Privacy and data retention](004-privacy-and-retention.md).

## Persistent job lifecycle

| Status | Meaning and transitions |
| --- | --- |
| `pending` | Waiting for an eligible Worker and retry time; can be claimed or cancelled. |
| `running` | Atomically owned by one Worker; can complete, fail, retry, or observe cancellation. |
| `completed` | Final file and catalog record were committed. Terminal. |
| `failed` | A permanent error occurred or the attempt limit was exhausted. Terminal. |
| `cancelled` | Pending work was cancelled, or a running Worker stopped and discarded its partial file. Terminal. |

One SQL statement claims the oldest eligible job. Worker-owned updates require both
job ID and Worker ID, preventing a stale Worker from completing another Worker's job.
Retryable failures use bounded exponential delay, and Telegram flood waits are honored
when longer. Byte-range resume is not guaranteed: interrupted partial bytes are
discarded and an eligible job starts another attempt.

On startup, persisted cancellation requests become cancelled, eligible interrupted
jobs return to pending with `resume_required` set, and exhausted jobs fail. These
paused jobs cannot be claimed until their requester selects Continue. After recovery
and once Telegram clients can send, each configured authorized user receives one
best-effort initialization message containing the package version, a server-local ISO
timestamp with timezone, and that requester's interrupted count. A positive count
adds persistent `View interrupted tasks` and `Continue interrupted tasks` buttons;
zero states that no interrupted tasks exist and omits those actions. Continue calls
the Job API for that requester only, clears only their paused jobs, wakes Workers, and
reports the resumed count. Ordinary pending jobs continue normally, repeated restarts
leave uncontinued jobs paused, and a failure to message one user does not abort
startup. Persistent interaction rows remain usable until expiry. Filesystem
organization intents are reconciled before normal service startup so a crash between
a move and its database update can be recovered.

## Local storage and catalog

The configured download root uses this layout:

```text
downloads/
├── inbox/
├── archive/
└── .partial/
```

- Workers reserve collision-safe Inbox names in SQLite before writing.
- Download bytes and crash-recovery state stay in a hidden per-job partial area.
- Completion uses synchronized, no-overwrite filesystem operations before the job and
  shared catalog are committed in SQLite.
- Rename and archive operations validate relative paths, reject symbolic links and
  unexpected file types, preserve the download-root boundary, and use a short job-ID
  suffix when a destination name collides.
- Startup reconciles tracked v0.1 UUID-directory outputs into the Inbox one at a time.
  Ambiguous, changed, unsafe, or conflicting files are preserved for operator review.

The database catalog is the authoritative index of completed files; the filesystem
and database must be backed up and restored as one consistency unit.

## Configuration and deployment

TGHarbor requires Python 3.15 or newer. Strict YAML configuration defines Telegram
credentials and authorized users, download behavior, database/session paths, and log
level. Supported environment variables can override configured values. Unknown or
invalid values stop startup, and secret fields are excluded from object
representations.

`tgharbor login` authorizes the user session in a trusted terminal. `tgharbor run`
starts migration/recovery, Telegram clients, a best-effort Bot command-menu
registration, Workers, best-effort startup notices, and Bot handling. Command-menu
registration covers the default and all-private-chat scopes independently; a scope
failure is logged without user IDs and never blocks startup or typed commands. A
database-adjacent process lock prevents a second application instance from using the
same state. The supplied container runs as a non-root user; do not scale it above one
replica.

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

- Channel/chat history crawling, message ranges, bulk import, or expanding a single
  linked album member into the whole remote album.
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
- [Telegram interaction UX](../1-architecture/103-telegram-interaction-ux.md)
- [v0.2 implementation and acceptance](../2-implementation/202-v0-2-library-workflow.md)
- [Core Downloader operations](../3-operations/301-core-downloader-operations.md)
