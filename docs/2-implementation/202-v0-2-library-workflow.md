# 202 — v0.2 Library workflow

[简体中文](../zh-CN/2-implementation/202-v0-2-library-workflow.md)

## Scope

v0.2 improves open-source release safety and adds a readable shared library without
changing the v0.1 Provider boundary.

Included: bounded display metadata; active-only status and paginated history;
shared Inbox/Archive catalog; collision-safe flat completed downloads; safe rename,
archive, directory creation, listing, and statistics; persistent inline workflows
with operator audits; schema migration; legacy-output reconciliation; and public
release security gates.

Excluded: channel history crawling, arbitrary external URLs, cross-filesystem
archive roots, role-based access beyond authorized users, and complete Telegram
message bodies.

## Delivery sequence

1. Publish privacy, migration, authorization, and rollback contracts.
2. Audit the tree, history, refs, image, and release payload; rotate exposed secrets.
3. Add release security automation.
4. Add schema v2 and migration tests.
5. Add bounded Provider metadata and filtered/paginated queries.
6. Add storage layout, catalog, organization service, and legacy reconciliation.
7. Add persistent Telegram interactions, readable rendering, and commands.
8. Reorganize public documentation and run complete verification.
9. Back up deployed state, rehearse migration, deploy, and verify rollback.

## Acceptance

- No tracked file, Git object, remote ref, image layer, generated configuration, or
  release payload contains a real credential, Telegram session, user content, or
  private deployment identifier.
- CI uses least privilege and runs lint, tests, secret scanning, and dependency
  checks for pushes and pull requests.
- A v1 database upgrades to v2 without changing existing job state; newer schemas
  remain rejected.
- Display text is normalized and at most 160 Unicode characters. Complete message
  bodies and sensitive Telegram objects are absent from persistence and logs.
- New downloads complete into `inbox/`; temporary bytes remain under `.partial/`;
  same-name files never overwrite each other.
- Interrupted completion or organization can be reconciled without deleting user
  content. Failed legacy items remain at their previous paths for review.
- `/status` excludes terminal jobs. History, files, directories, and statistics are
  bounded or paginated and use readable names and sizes.
- Every authorized user can organize the shared library. Rename, archive, and
  directory creation record the operator ID.
- Completion actions survive restart and reject unauthorized, expired, stale, or
  conflicting input explicitly.
- Tests reject traversal, absolute paths, symbolic links, ambiguous short IDs,
  unexpected file types, and overwrite attempts.
- English and Chinese documentation, navigation, examples, operations guidance, and
  the local handoff are synchronized.

## Deployment and rollback

Stop the service and back up the SQLite database, Telegram sessions, and download
root. Rehearse migration and reconciliation against a copy. Report counts and
failures without logging content or private Telegram references.

After deployment, verify schema version, file counts, permissions, active jobs,
shared commands, callback recovery, and container stability. Rollback means stopping
the new version and restoring database and download-root backups together.
