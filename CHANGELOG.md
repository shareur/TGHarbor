# Changelog

This project follows a Keep a Changelog style while it remains pre-1.0.

## Unreleased

## 0.2.3 - 2026-11-24

This release changes the public release line substantially relative to the published
`0.2.2` source snapshot. The Telegram interaction workflow, durable interaction
storage, and album batch notifications described below shipped in the `0.2.1`
container image line but were absent from the `0.2.2` source snapshot. `0.2.3`
restores that production-compatible behavior to the public release line, so the
previous snapshot's queue, prompt, result, and notification behavior is replaced
rather than preserved.

### Added

- Integrated one-card, edit-first Telegram interaction workflow for keep, rename,
  archive, rename + archive, directory choice, new-directory prompts, confirmation,
  visible working state, and results. These actions edit the originating message and
  fall back to exactly one replacement only when the original is invalid, missing,
  not authored by the Bot, or no longer editable.
- Durable prompt delivery: a text reply edits its stored prompt message, prompt
  anchors are rebound with an expected-value compare-and-swap, invalid or
  slash-prefixed folder input stays on the prompt, and one active text workflow per
  actor is enforced.
- Durable result and retry delivery: a committed single-file or terminal batch
  organization keeps a `result-pending` row, and a partial batch keeps a
  `retry-pending` row, until the final card or Retry card is known delivered. A send
  return without a positive message identity is ambiguous and keeps the row pending;
  startup restores the card from current catalog state without rerunning the
  organization.
- One at-least-once album batch notification per requester, including startup redrive
  of unsent notices and a persistent `Retry move` action after a partial batch move.
- Startup recovery that marks eligible interrupted jobs paused, then sends each
  authorized user one best-effort message with the version, a server-local timestamp
  with timezone, and their own paused count. View is requester-scoped, Continue
  resumes only that requester's paused jobs and wakes workers, and repeating Continue
  reports zero without error.
- Transactional schema v2-to-v4 upgrade chain (v2 → v3 → v4) applied automatically at
  first start, adding the interaction prompt-message anchor plus in-place pagination
  and durable result-delivery state. Each step runs in one explicit SQLite
  transaction, and a failed step rolls itself back and stops initialization.
- Startup commands, callback routing, authorization, and expiry for the restored
  interactions, including per-session failure isolation that cannot abort startup or
  change job state.

### Changed

- Limit Telethon's logger namespace to `ERROR` or stricter so `logging.level` no
  longer enables high-volume Telethon `INFO` traffic. TGHarbor's own loggers keep
  following the configured level, and propagation is unchanged.
- Report SQLite and storage-adapter failures with sanitized persistence diagnostics:
  a fixed category, exception class name, and numeric SQLite primary code or `errno`,
  without raw exception text, SQL, parameters, paths, or a chained cause. Transient
  categories (`busy`, `io`, `disk-full`, `permission`, `read-only`, `open`) keep the
  existing retry/backoff behavior, while permanent categories (`corrupt`, `schema`,
  `integrity`, `adapter`, and the `database` fallback) fail the job instead of
  repeating an unrecoverable operation. A worker-pool persistence failure no longer
  stops that worker, repeated identical permanent failures are logged once per
  fingerprint, and repeated transient failures are aggregated instead of flooding the
  log.
- Persist provider failure details only when they exactly match the fixed messages
  used by the built-in Telegram provider; any other provider message is reduced to
  the exception class name before it is logged or stored.
- Raise the SQLite busy timeout to 15 seconds for every connection, including
  initialization, to tolerate short writer contention in the default rollback-journal
  mode.

### Fixed

- Downloads without a Telegram filename now derive a safe extension from the media
  extension or MIME type instead of always writing `.bin`.
- Detailed completion and failure summaries include the short job ID as `Job`
  immediately after the title, and Keep preserves the completion summary while
  stating that the file remains in Inbox.
- Directory, confirmation, cancellation, partial-failure, and stale/expired feedback
  edit the originating card instead of creating a chain of new messages.
- A failed, cancelled, or interrupted final edit or send no longer loses the only
  recoverable result, and the library mutation and pending transition are one
  cancellation-safe critical section.

### Notes

- An installation upgrading from the published `0.2.2` release (schema 2) upgrades to
  schema 4 automatically on first start: back up the database, sessions,
  configuration, and complete download root first, and follow
  [303 — v0.2 upgrade and rollback](docs/3-operations/303-v0-2-upgrade.md). An
  existing schema-4 installation starts in place with no migration step, no
  configuration change, and no new configuration key. A database whose schema version
  is newer than 4 is refused.
- The tests were extended for the migration chain, interaction state transitions,
  batch notifications, pause/continue recovery, persistence diagnostics and log
  hygiene, and the publication, distribution, and release metadata gates.
- Container images: the `0.2.3` release publishes `0.2.3`, `0.2`, `latest`, and
  `sha-<commit>` tags for `linux/amd64` and `linux/arm64` from the release tag. Tags
  remain mutable aliases; the published image digest is the immutable identity. The
  recorded `0.2.1` image digest remains an immutable historical record.

## 0.2.2 - 2026-11-13

### Changed

- Formatting-only source maintenance release that restores the repository formatting
  gate on the initial public source snapshot. It changes no runtime behavior, adds no
  container image, and does not contain the Telegram interaction workflow described
  under `0.2.1`. No tag was moved and no image was republished.

## 0.2.1 - 2026-11-07

### Added

- Persistent Telegram interaction state so keep, rename, archive, directory,
  confirmation, and pagination actions survive process restarts.
- Startup recovery controls that show or continue only the requester's interrupted
  paused jobs.
- Detailed completion notices with the final catalog name, logical folder, stored
  size, server-local timing, speed, requester, and source.
- Album `Move all to new folder` action that rejects stale selections and keeps a
  persistent retry action after partial results.
- Durable at-least-once delivery of one per-requester album batch notice, including
  startup redrive of unsent notices.
- Integrated one-card Telegram UX: completion-card actions edit the originating
  message through directory choice, new-directory prompt, confirmation, visible working
  state, and result; Back and Cancel controls; and a single replacement send when an edit
  is unavailable.
- Directory chooser context, recent-use ordering, explicit empty state, and
  relative-path validation that keeps invalid or slash-prefixed input in the waiting
  step without creating a job.
- One active text workflow per actor, with deterministic routing before link
  submission and `/cancel` support for the active prompt.
- Best-effort Bot command-menu registration for default and all-private-chat scopes
  with per-scope failure isolation.
- Transparent intake messages with source, pending status, and next `/status`
  guidance, plus album count and Move all timing.
- Transactional schema v1/v2/v3-to-v4 migration, including the interaction prompt-
  message anchor for edit-first prompts and in-place pagination.

### Fixed

- Downloads without a Telegram filename now derive a safe extension from the media
  extension or MIME type instead of always writing `.bin`.
- Detailed completion and failure summaries now include the short job ID as `Job`
  immediately after the title, and Keep preserves the completion summary while stating
  that the file remains in Inbox.
- Directory, confirmation, cancellation, partial-failure, and stale/expired feedback no
  longer creates a chain of new messages when the originating card can be edited.
- A committed single-file or batch organization keeps a durable `result-pending` or
  `retry-pending` interaction row until the final result card is known delivered, so a
  failed, cancelled, or interrupted final edit or send no longer loses the only
  recoverable result. The library mutation and pending transition are one
  cancellation-safe critical section, including new-directory creation and batch
  organization; an outer cancellation settles them before propagating. A send return
  without a positive message identity is treated as ambiguous and keeps the row
  pending. Restart reconstructs the card from current catalog state without rerunning
  organization, renews recovered working and retry expiries, lists all pending rows
  without a fixed backlog cap, retries move only remaining Inbox members, and repeated
  identical moves or directory creations do not duplicate audit rows. The schema
  remains v4 with no migration required.

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
