# 303 — v0.2 upgrade and rollback

[简体中文](../zh-CN/3-operations/303-v0-2-upgrade.md)

This guide upgrades SQLite schema 1, 2, or 3 to 4 and moves tracked completed v0.1
UUID-directory outputs into the shared Inbox. Schema v4 adds the nullable interaction
prompt-message anchor used by edit-first prompts and in-place pagination. Durable
result delivery reuses those interaction rows and step values only; the schema remains
version 4 and no migration is required for it. Rehearse this procedure on copies before
production.

An installation that already uses schema 4 needs no migration step. Back up and stop the
service as described below, then start the 0.2.3 build; initialization confirms schema
version 4 and changes no table. A build that supports schema 4 still refuses a database
whose `user_version` is newer than 4 instead of guessing at an unknown layout.

## Upgrading from the published v0.2.2 release

A `0.2.2` installation stores SQLite schema 2. The first `0.2.3` start upgrades it to
schema 4 automatically through the v2 → v3 → v4 chain. There is no manual SQL step, no
migration command, and no new configuration key.

1. Complete the backup described in "Before upgrade" first. That restore point must
   exist and be verified before the first `0.2.3` start, because the upgrade is not
   reversible in place.
2. Start `0.2.3` against the existing data directory. Initialization applies one schema
   version per explicit transaction and stops at the first failed step after rolling
   that step back, leaving the last completed version in place.
3. Compatibility is one-way. A `0.2.2` build cannot open a schema-4 database, so a
   rollback means restoring the pre-upgrade database, sidecars, sessions,
   configuration, and complete download root together from the backup, and then
   starting `0.2.2` again. Do not run different versions against one data directory,
   and do not copy a schema-4 database onto a pre-upgrade download root.
4. Verify the upgrade with the checks below before returning the service to users.

Upgrade verification for this path:

- The database reports schema version 4 and startup logs no migration error.
- `/status` lists the requester's jobs, and jobs interrupted by the upgrade appear as
  paused until their requester continues them.
- Each authorized user receives one startup message with the package version, a
  server-local timestamp with timezone, and their own paused count. Continue resumes
  only that user's jobs, and a repeated Continue reports zero without error.
- A test download enters Inbox, and keep, rename, archive, directory, and batch actions
  edit the originating card and survive a restart.
- A completed multi-member album produces exactly one batch notification per requester,
  with a persistent Retry action after a partial move.
- Legacy reconciliation reports no unexplained failures, and file and byte totals match
  the backup inventory.
- Interaction state, pending results, and retry cards are restored after restart
  without rerunning organization.

The longer checklist under "Production verification" covers the remaining interaction,
pagination, and delivery checks for this release.

## Before upgrade

1. Stop TGHarbor and confirm no process owns the instance lock.
2. Back up the SQLite database and sidecars, Telegram sessions, configuration, and the
   complete download root while preserving ownership and permissions.
3. Keep the database and download-root backups as one restore point.
4. Install the candidate build containing schema v4 in an isolated environment and
   scan the release/image. For a container upgrade, verify that the image tag resolves
   to the recorded candidate digest and that its OCI revision label is the exact
   candidate commit; the tag alone is not provenance.

## Rehearsal

Run the new application against copied state. Initialization applies one schema
version per explicit SQLite transaction; a failed step rolls that step back and
stops initialization. Startup then recovers pending organization intents and
reconciles completed legacy files one at a time. It never overwrites an unrelated
Inbox file; ambiguous directories, symlinks, changed files, and conflicts remain
unchanged for review.

Record only schema version, discovered/succeeded/failed counts, file counts, and
permission results. Do not copy filenames, message summaries, private links, or raw
database rows into public logs or issues.

## Production verification

- Schema version is 4 and the service starts without migration errors.
- Interactions migrated from schema v3 have a NULL prompt anchor, and new prompts
  acquire one without exposing message IDs in callbacks or logs.
- `/files` and `/history` Next and Previous edit the originating list message; a
  successful edit adds no duplicate, a replayed page button is stale, an uneditable
  message produces exactly one replacement list with valid buttons, and a timeout or
  ambiguous edit failure keeps the old page usable without a duplicate send. A page
  callback atomically consumes its exact authorized, unexpired interaction before
  creating successors, so concurrent callbacks produce one edit, one successor set,
  and stale losers. Successors start anchored to the callback message, so a same-message
  edit needs no rebind. Delivery is shielded and committed before cancellation is
  re-raised. A fallback replacement moves every successor anchor in a shielded critical
  section; if that rebind fails, the replacement is disabled, successors are removed,
  the exact original interaction is restored, and its source token is re-rendered (or
  one retry card is sent and bound). A pre-delivery failure or cancellation deletes
  successors and restores the exact original interaction, unless it expired during
  delivery. Every visible page token should resolve to a live persisted row.
- A text reply edits its stored prompt message when possible. Only a provably
  uneditable message falls back to exactly one replacement; prompt anchor rebinding
  is a compare-and-swap against the expected current anchor, and a losing replacement
  is disabled without replacing the live card. Every retained Back, Cancel, Retry,
  directory, or pagination card uses the same helper, so clicking a control on the
  replacement works.
- Completion-card actions edit the originating message through the directory chooser,
  new-directory prompt, confirmation, visible working state, and result. Back returns
  to the chooser, Cancel works at chooser/prompt/confirmation, invalid or
  slash-prefixed folder input stays on the prompt with the relative-path example, and
  Keep preserves the completion summary. A duplicate confirmation callback reports
  processing while only one library operation runs; recoverable failures restore
  confirmation or retry state with Back/Cancel or Retry/Cancel controls. After a
  working claim, the library mutation and its pending transition are one cancellation-
  safe critical section: an outer cancellation after work begins settles the operation
  and pending write before propagating, a pre-commit failure may restore the prior
  confirmation and anchor, and a committed effect never restores a duplicate action.
  This is an asynchronous cancellation guarantee only; SIGKILL or power loss between
  filesystem and catalog boundaries relies on idempotent reconciliation and replay.
  Restart atomically restores any persisted `working-confirm`/`working-confirm-new-
  directory` row to its prior confirmation and a legacy generic `working` row to
  `ready`, including a claim whose original expiry passed, with a renewed actionable
  lifetime; the restored sessions are returned before callbacks are handled. After
  Telegram startup the bot edits each
  recovered anchor into a visible card: confirmations show pending name/folder context
  with Confirm, Back, and Cancel; a new directory shows Create and move; legacy ready
  rows show an interrupted-action card with the appropriate Choose folder or Retry
  move control and Cancel. An absent or provably uneditable anchor gets one replacement
  send bound by compare-and-swap. Per-session failures log only step, outcome,
  category, and error type, do not abort startup or change job state, and leave the
  restored row recoverable through `/files`, `/history`, `/dirs`, `/rename`, or
  `/archive`.
- A successful single-file or terminal batch organization keeps a non-actionable
  `result-pending` row until its final card is known delivered. A partial batch result
  keeps `retry-pending` until its Retry card is known delivered and its actual anchor is
  compare-and-swap bound, then becomes actionable `ready` with a renewed actionable
  expiry. A forced terminal edit or replacement send failure after commit must leave
  the pending row and no duplicate replacement; a send return without a positive
  message identity is ambiguous and keeps it pending; an ambiguous failure keeps it for
  the next start. Restart restores the card from current catalog state without rerunning
  organization, and a retry moves only remaining Inbox members. All pending rows are
  listed with no fixed backlog cap, so a repeatedly failing row cannot starve later
  rows. Unauthorized pending rows are skipped and retained. Repeated identical moves
  and repeated creation of an already-existing directory do not duplicate audit rows.
  The schema stays version 4 and no migration is required.
- Only one text workflow is active per actor. Atomic activation leaves page buttons and
  other completion cards usable, concurrent activation never leaves zero live
  workflows, and `/cancel` without a job token cancels the active prompt.
- Command-menu registration reports the default and all-private-chat scopes. A forced
  scope failure logs only outcome, scope, and error type while startup and typed
  commands continue; no user IDs appear in logs.
- Legacy reconciliation reports no unexplained failures.
- File and byte totals match the rehearsal and backup inventory.
- `/status`, `/history`, `/files`, `/dirs`, and `/stats` return expected scoped data.
- A test download enters Inbox and its keep/rename/archive actions survive restart.
- Rename/archive audit records contain the acting authorized Telegram user ID.
- Interrupted eligible jobs are counted as paused pending jobs and are not claimed
  until their requester continues them; exhausted and cancelled interrupted jobs are
  terminal.
- Each configured authorized user receives one best-effort startup message with the
  package version, a server-local ISO timestamp with timezone, and their own paused
  count. View is requester-scoped; Continue resumes only that user's paused jobs and
  wakes Workers. Repeating Continue reports zero without error.
- A completed multi-member album produces at most one Move all interaction. Existing
  directories appear in recent-use order plus New directory, and confirmation reports
  moved, skipped, and failed counts without host paths. Directory buttons are bound to
  the displayed directory and ordering, so a changed list rejects the old choice as
  stale, and a partial result keeps a persistent `Retry move` action.
- A successful completion message shows the final catalog name, logical
  `/download/<relative parent>` folder, stored size, server-local start/end times,
  elapsed time, average speed, requester, and origin/channel or `N/A`.

## Schema-v4 backup and rollback

A schema-v4 backup is one restore point containing the SQLite database and sidecars,
Telegram sessions, configuration, and the complete download root, including Inbox,
Archive, partial/recovery state, batch-notification state, interaction rows with prompt
anchors, and user files. Verify checksums before restoring and preserve ownership and
permissions.

Stop the new process. Restore the pre-upgrade SQLite database, sidecars, and complete
download root together, then restore the prior application version. Never run an
older application against a schema-4 database or mix a pre-upgrade database with a
post-reconciliation download root. Do not copy a schema-4 database onto a v0.1/v0.2
download root or vice versa. Keep failed rehearsal/production state for private
diagnosis rather than deleting ambiguous user content.

The command menu is best-effort client metadata, not application state. The prior
application does not register the integrated menu; Telegram may keep showing the last
registered menu until that client clears or replaces it. Re-registration by a later
version reconciles the menu. If the menu is missing or incomplete, check only the
outcome/scope/error-type warning and retry by reconnecting or restarting; do not add
user IDs to logs.

If a recovered confirmation or Working card is missing after restart, check for
`outcome=not-visible` with a step, category, and error type. The interaction row is
already restored and job state is unchanged; continue with `/files` or `/history` and
then `/rename`, `/archive`, `/dirs`, or `/mkdir`. If a page card shows stale controls,
use the visible Retry control or re-run `/files`/`/history`. Do not edit interaction
rows manually and do not claim visible recovery succeeded until the card appears.
