# 004 — Privacy and data retention

[简体中文](../zh-CN/0-foundation/004-privacy-and-retention.md)

TGHarbor is self-hosted and does not provide a hosted privacy service. The operator is
responsible for lawful use, Telegram account permissions, filesystem access, backups,
retention, deletion, and disclosure to authorized users.

## Persisted data

- SQLite stores job source identifiers, requester IDs, status/progress/retry state,
  output paths, original/current filenames, MIME type, file size, and a whitespace-
  normalized Telegram message summary limited to 160 Unicode characters.
- The shared catalog stores Inbox/Archive paths and the latest operator ID.
- Audit rows store actor IDs, action, source/destination paths, and time.
- Interaction sessions store actor, target file, pending name/directory, step, and
  expiry. They do not store callback secrets.
- Telegram session files authenticate the Bot and user account; downloads contain user
  content. Both are sensitive.

TGHarbor does not intentionally persist complete Telegram message bodies or raw
Telegram objects. It does not implement content search, content-hash deduplication, or
automatic retention deletion.

## Access boundaries

Jobs and cancellation are requester-scoped. All configured authorized users can read
and organize the shared file catalog. Operators must configure that list accordingly;
TGHarbor is not a multi-tenant isolation boundary for the library.

## Retention and deletion

Completed jobs, catalog rows, audits, interactions, sessions, and downloaded files do
not currently have an automatic retention policy. Removing only a file or only a
database row can create inconsistency. Stop the service and use a tested operator
procedure when deleting or restoring data. Back up and restore the SQLite database and
download root together.

Before publishing diagnostics, repositories, archives, or images, remove and scan for
credentials, sessions, message summaries, private links, filenames, database content,
logs, downloads, and deployment identifiers. Follow the release-security guide.
