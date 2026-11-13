# Changelog

This project follows a Keep a Changelog style while it remains pre-1.0.

## Unreleased

## 0.2.2 - 2026-11-13

### Changed

- Apply Ruff formatting to the release validation scripts and release metadata tests so
  the repository-wide formatting check passes on the published source snapshot.
- Align project and runtime version metadata with 0.2.2.

Formatting-only source release: no runtime, configuration, interface, or documented
behavior change. No container image is published for 0.2.2; the 0.2.1 Docker Hub image
and its aliases remain the current image release, and the release workflow refuses to
overwrite them.

## 0.2.0 - 2026-11-07

### Added

- Shared Inbox/Archive file catalog and authorized-user organization commands.
- Readable bounded Telegram metadata and human-readable size rendering.
- Persistent inline keep, rename, archive, directory, confirmation, cancellation, and
  pagination interactions.
- Transactional schema v1-to-v2 migration, legacy storage reconciliation, and
  organization crash recovery.
- Release security workflow, dependency auditing, and open-source community files.

### Changed

- `/status` now lists only requester-owned pending/running jobs; terminal results use
  `/history`.
- Completed files now enter a shared flat Inbox instead of per-job UUID directories.

## 0.1.0

- Initial dual-session Telegram downloader, persistent queue, Worker pool, safe local
  storage, Bot status/cancel commands, and Docker deployment configuration.
