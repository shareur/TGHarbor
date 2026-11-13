# 102 — File catalog and interactions

[简体中文](../zh-CN/1-architecture/102-file-catalog-and-interactions.md)

TGHarbor v0.2 separates download execution from post-download organization. A job
describes acquisition and retry state; a stored file describes the current file name,
location, and organization history.

## Privacy and metadata

The Telegram Provider may expose normalized message text truncated to 160 Unicode
characters, the original file name, MIME type, and size. TGHarbor does not persist the
complete message body. The bounded title is presentation metadata, not a file name.
Logs must not contain message text, Telegram links, credentials, access hashes, or raw
Telegram objects.

## Authorization and catalog state

Job status and cancellation remain requester-scoped. All authorized users can list and
organize the shared catalog; rename, archive, and directory creation record the acting
Telegram user ID. Shared-library access never implies ownership of another user's job,
and the library is not a multi-tenant isolation boundary.

Completed-file records live in SQLite alongside the download root. Upgrades preserve
existing job and catalog state, and unsupported newer state is refused rather than
modified. Back up the database and download root together before an upgrade.

## Storage layout

```text
downloads/
├── inbox/       completed files awaiting organization
├── archive/     authorized-user managed directories
└── .partial/    hidden, per-job temporary files
```

Desired names are sanitized. A conflict appends a short job identifier before the
extension; unrelated files are never overwritten. All paths remain below the root.
Absolute paths, traversal, empty segments, symbolic-link components, and unexpected
file types are rejected. Completion and organization leave either a complete cataloged
result or a recoverable file for operator review; interruption does not delete user
content.

## Bot interactions and queries

Completion actions are Keep, Rename, Archive, and Rename + archive. Directory selection,
creation, confirmation, cancellation, and pagination remain usable across a normal
restart and expire after 24 hours. Every action rechecks the acting user's authorization,
expiry, and current catalog state; unauthorized, expired, stale, or conflicting actions
fail visibly.

- `/status` lists requester-owned pending/running jobs.
- `/history` lists requester-owned terminal jobs with pagination.
- `/files`, `/dirs`, and `/stats` read the shared catalog.
- `/rename`, `/archive`, and `/mkdir` mutate the shared catalog for an authorized user
  and record that user's Telegram ID.

Short job identifiers are accepted only when they uniquely resolve inside the command's
authorization scope. See the [Bot command reference](../0-foundation/003-command-reference.md)
for syntax.

## Backup and rollback

Stop the service before a filesystem-level backup. Keep the SQLite database, its
sidecars, Telegram sessions, and the complete download root in the same restore point.
Rollback means stopping the new version and restoring those backups together; restoring
only one side can orphan or misidentify files. Follow the
[v0.2 upgrade and rollback guide](../3-operations/303-v0-2-upgrade.md).
