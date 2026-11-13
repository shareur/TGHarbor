# 303 — v0.2 upgrade and rollback

[简体中文](../zh-CN/3-operations/303-v0-2-upgrade.md)

v0.2 upgrades SQLite schema 1 to 2 and moves tracked completed v0.1 UUID-directory
outputs into the shared Inbox. Rehearse this procedure on copies before production.
Version `0.2.2` is a formatting-only source release; the current container image remains
the recorded `0.2.1` publication.

## Before upgrade

1. Stop TGHarbor and confirm no process owns the instance lock.
2. Back up the SQLite database and sidecars, Telegram sessions, configuration, and the
   complete download root while preserving ownership and permissions.
3. Keep the database and download-root backups as one restore point.
4. For a source installation, install version `0.2.2` in an isolated environment and
   scan the built artifacts. `0.2.2` is a formatting-only source release with no
   container image, so it changes no runtime behavior and adds no migration step beyond
   the schema 1-to-2 upgrade described here.
5. For a container upgrade, pull `shareur/tgharbor:0.2.1` from
   [Docker Hub](https://hub.docker.com/r/shareur/tgharbor), verify that it resolves to
   the recorded candidate digest, and confirm that its OCI revision label is the exact
   candidate commit; the tag alone is not provenance. Container deployment stays on the
   recorded `0.2.1` image because no `0.2.2` image is published.

## Rehearsal

Run the new application against copied state. Initialization performs the schema
migration in one SQLite transaction. Startup then recovers pending organization
intents and reconciles completed legacy files one at a time. It never overwrites an
unrelated Inbox file; ambiguous directories, symlinks, changed files, and conflicts
remain unchanged for review.

Record only schema version, discovered/succeeded/failed counts, file counts, and
permission results. Do not copy filenames, message summaries, private links, or raw
database rows into public logs or issues.

## Production verification

- Schema version is 2 and the service starts without migration errors.
- Legacy reconciliation reports no unexplained failures.
- File and byte totals match the rehearsal and backup inventory.
- `/status`, `/history`, `/files`, `/dirs`, and `/stats` return expected scoped data.
- A test download enters Inbox and its keep/rename/archive actions survive restart.
- Rename/archive audit records contain the acting authorized Telegram user ID.

## Rollback

Stop the v0.2 process. Restore the pre-upgrade SQLite database, sidecars, and complete
download root together, then restore the prior application version. Never run v0.1
against a schema-2 database or mix a pre-upgrade database with a post-reconciliation
download root. Keep failed rehearsal/production state for private diagnosis rather
than deleting ambiguous user content.
