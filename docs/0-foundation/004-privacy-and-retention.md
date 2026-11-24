# 004 — Privacy and data retention

[简体中文](../zh-CN/0-foundation/004-privacy-and-retention.md)

TGHarbor is self-hosted and does not provide a hosted privacy service. The operator is
responsible for lawful use, Telegram account permissions, filesystem access, backups,
retention, deletion, and disclosure to authorized users.

## Persisted data

- SQLite stores job source identifiers, requester IDs, status/progress/retry state,
  output paths, original/current filenames, MIME type, file size, optional opaque batch
  identity and expected size, attempt start/finish timestamps, resume state, and
  optional original group/channel metadata. A source channel may populate both the
  original group and channel fields; a basic group or unavailable peer populates only
  what Telegram supplies, and missing values render `N/A`. It also stores a whitespace-
  normalized Telegram message summary limited to 160 Unicode characters.
- The shared catalog stores Inbox/Archive paths and the latest operator ID.
- Audit rows store actor IDs, action, source/destination paths, and time.
- Interaction sessions store actor, target file or opaque batch ID, pending
  name/directory, step, and expiry. Callback payloads contain only an interaction ID and
  a step-bound action token, which for a directory button includes an opaque short
  digest; they never contain the batch ID, a directory name or path, or source
  components.
- `batch_notifications` rows store requester, opaque batch ID, claim/send state, and
  timestamps so a successful album completion is announced at most once.
- Telegram session files authenticate the Bot and user account; downloads contain user
  content. Both are sensitive.

TGHarbor does not intentionally persist complete Telegram message bodies or raw
Telegram objects. It does not implement content search, content-hash deduplication, or
automatic retention deletion.

## Access boundaries

Jobs, cancellation, paused-job views, continuation, and batch lookup/movement are
requester-scoped. All configured authorized users can read and organize the shared file
catalog. Operators must configure that list accordingly; TGHarbor is not a multi-tenant
isolation boundary for the library.

## Retention and deletion

Completed jobs, catalog rows, audits, interactions, batch-notification state, sessions,
and downloaded files do not currently have an automatic retention policy. Removing only
a file or only a database row can create inconsistency. Stop the service and use a
tested operator procedure when deleting or restoring data. Schema-v4 backups and
rollbacks must restore the SQLite database, sidecars, and complete download root as one
consistency unit.

Before publishing diagnostics, repositories, archives, or images, remove and scan for
credentials, sessions, message summaries, private links, filenames, database content,
logs, downloads, and deployment identifiers. Follow the release-security guide.
