# 102 — File catalog and interactions

[简体中文](../zh-CN/1-architecture/102-file-catalog-and-interactions.md)

TGHarbor v0.2 separates download execution from post-download organization. A job
describes acquisition and retry state; a stored-file record describes the current
file name, location, and organization history.

## Privacy and metadata

The Telegram Provider may expose normalized message text truncated to 160 Unicode
characters, the original file name, MIME type, and size. TGHarbor does not persist
the complete message body. The bounded title is presentation metadata, not a file
name. Logs must not contain message text, Telegram links, credentials, access hashes,
or raw Telegram objects.

## Persistent model

Schema version 2 adds bounded display metadata and a reserved Inbox name to jobs. It
also introduces:

- a **stored file** for each completed output, with a root-relative path,
  original/current name, size, MIME type, Inbox/Archive state, and latest operator;
- an **interaction session** containing an authorized actor, action, step, target
  file, pending input, and expiry;
- an **audit event** containing actor, operation, source/destination paths, and time.

All authorized users can list and organize the shared catalog. Job status and
cancellation remain requester-scoped. Migrations run one version at a time in
explicit transactions, preserve existing rows, and reject newer schemas. Rollback
after migration requires restoring the database and download-root backup together.

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
file types are rejected. Completion and organization use same-filesystem atomic
moves and directory synchronization.

The Worker resolves and persists metadata, reserves an Inbox name, downloads to the
job's hidden partial path, atomically commits to Inbox, creates the catalog record,
then publishes actions. Rename/archive operations persist intent before moving; on
restart, recovery finalizes a provable result or reports a conflict without deleting
content.

Existing v0.1 UUID directories are legacy outputs. Reconciliation only handles
completed jobs with tracked regular files, reserves collision-safe names, moves one
file at a time, and leaves ambiguous or failed items unchanged for operator review.

## Bot interactions and queries

Completion actions are `keep`, `rename`, `archive`, and `rename-and-archive`.
Directory selection, creation, confirmation, cancellation, and pagination are
persistent interaction steps. Callback data carries only an opaque interaction ID
and action token. Every callback rechecks authorization, expiry, and catalog state.

- `/status` lists requester-owned pending/running jobs.
- `/history` lists requester-owned terminal jobs with filters and pagination.
- `/files`, `/dirs`, and `/stats` read the shared catalog.
- `/rename`, `/archive`, and `/mkdir` mutate the shared catalog for an authorized
  user and record that user's Telegram ID.

Short job identifiers are accepted only when they uniquely resolve inside the
command's authorization scope.
