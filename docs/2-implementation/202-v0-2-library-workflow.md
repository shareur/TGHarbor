# 202 — v0.2 Library workflow

[简体中文](../zh-CN/2-implementation/202-v0-2-library-workflow.md)

## Scope

v0.2 improves public release safety and adds a readable shared library without changing
the v0.1 Provider boundary.

Included: bounded display metadata; active-only status and paginated history; a shared
Inbox/Archive catalog; collision-safe completed downloads; safe rename, archive,
directory creation, listing, and statistics; persistent inline workflows with operator
audits; a safe upgrade path for existing installations; and public release security
gates.

Excluded: channel history crawling, arbitrary external URLs, cross-filesystem archive
roots, role-based access beyond authorized users, and complete Telegram message bodies.

## Acceptance

- No tracked file, candidate archive, Git object, remote ref, image layer, generated
  configuration, or release payload contains a real credential, Telegram session, user
  content, or private deployment identifier.
- CI uses least privilege and runs lint, tests, secret scanning, dependency checks, and
  public-link validation for pushes and pull requests.
- An existing installation upgrades without changing existing job state; unsupported
  newer state is refused rather than modified.
- Display text is normalized and at most 160 Unicode characters. Complete message
  bodies and sensitive Telegram objects are absent from persistence and logs.
- New downloads complete into `inbox/`; temporary bytes remain under `.partial/`;
  same-name files never overwrite each other.
- Interrupted completion or organization is recovered when safe or preserved for
  operator review without deleting user content.
- `/status` excludes terminal jobs. History, files, directories, and statistics are
  bounded or paginated and use readable names and sizes.
- Every authorized user can organize the shared library. Rename, archive, and
  directory creation record the operator ID.
- Completion actions survive a normal restart and reject unauthorized, expired, stale,
  or conflicting input explicitly.
- Path traversal, absolute paths, symbolic links, ambiguous short IDs, unexpected file
  types, and overwrite attempts are rejected.
- English and Chinese documentation, navigation, examples, and operations guidance are
  synchronized.

## Deployment and rollback

Stop the service and back up the SQLite database, Telegram sessions, and download
root. Rehearse the upgrade against a copy. Report counts and failures without logging
content or private Telegram references.

After deployment, verify existing files and jobs, permissions, active jobs, shared
commands, completion actions, and container stability. Rollback means stopping the new
version and restoring database and download-root backups together.
