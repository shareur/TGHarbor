# 202 — v0.2 Library workflow

[简体中文](../zh-CN/2-implementation/202-v0-2-library-workflow.md)

## Scope

v0.2 improves open-source release safety and adds a readable shared library without
changing the v0.1 Provider boundary.

Included: bounded display metadata; active-only status and paginated history;
shared Inbox/Archive catalog; collision-safe flat completed downloads; safe rename,
archive, directory creation, listing, and statistics; persistent inline workflows
with operator audits; schema migrations through v4 with prompt-message anchoring;
edit-first callback feedback; durable result delivery that survives a failed or
cancelled final edit and restart; in-place pagination; attempt timing and paused
restart continuation; opaque batch identity, completion readiness, and durable notice
claiming; album intake and conservative original source metadata; detailed persisted
success messages; authorized all-member Inbox batch organization and Move all; startup
initialization plus View/Continue controls; legacy-output reconciliation; and public
release security gates.

Excluded: channel history crawling, arbitrary external URLs, cross-filesystem
archive roots, role-based access beyond authorized users, and complete Telegram
message bodies.

## Delivery sequence

1. Publish privacy, migration, authorization, and rollback contracts.
2. Audit the tree, history, refs, image, and release payload; rotate exposed secrets.
3. Add release security automation.
4. Add transactional schema migrations through v4 and migration tests.
5. Add bounded Provider metadata and filtered/paginated queries.
6. Add storage layout, catalog, organization service, and legacy reconciliation.
7. Add persistent Telegram interactions, readable rendering, and commands.
8. Add album intake, durable batch notices, detailed completion messages, and startup
   continuation controls.
9. Reorganize public documentation and run complete verification.
10. Back up deployed state, rehearse migration, deploy, and verify rollback.

## Acceptance

- No tracked file, Git object, remote ref, image layer, generated configuration, or
  release payload contains a real credential, Telegram session, user content, or
  private deployment identifier.
- CI uses least privilege and runs lint, tests, secret scanning, and dependency
  checks for pushes and pull requests.
- Schema-1, schema-2, and schema-3 databases upgrade transactionally to v4 without
  changing existing job state or interaction intent. Existing interaction rows receive
  a NULL prompt anchor. A failed v3-to-v4 step rolls back, and schema versions newer
  than v4 remain rejected.
- Eligible interrupted running jobs become paused pending jobs that normal claims
  skip. Exhausted jobs become failed and persisted cancellations become cancelled.
  Repeated restarts preserve the pause, and requester-scoped continuation is
  authorized, selective or complete, idempotent, and preserves retry accounting.
- Attempts record a fresh claim time and an atomic terminal finish time; completion
  uses the timestamp persisted in the completion transaction. The success message uses
  the final catalog name and logical `/download/<relative parent>`, stored size, and
  persisted start/end times converted to server-local `HH:MM:SS`; missing values and
  original source peers render `N/A`, and no host absolute path is exposed.
- Album intake submits every media member once with one opaque batch identity and the
  exact album size; grouped messages are not double-submitted. A single message remains
  one job. Batch identity and source metadata stay out of callbacks and logs. Group and
  channel IDs remain optional metadata, with no inference between them and `N/A` for
  private, unavailable, or ambiguous peers.
- A batch notice is claimed once only for an exactly complete successful batch of
  more than one member. Release, stale-claim recovery, sent-state idempotency, and a
  durable outbox redriven at startup and periodically prevent permanent loss; a
  claim is reclaimed atomically only after the dispatcher's configured stale
  interval. An interaction records intent and callback identity, not delivery
  evidence: a send or mark failure keeps and reuses the same interaction, and a
  retry redrives the notice at least once. Because the Telegram send and the SQLite
  mark are not atomic, a crash or failure between them can duplicate the chat
  notice, `sent` is terminal, and the adapter does not claim exactly-once delivery
  across both. The `Move all to new folder` flow lists existing directories in
  recent-use order plus New directory, confirms target selection and creation, and
  reports moved/skipped/failed counts without host paths.
- Authorized batch organization moves only current Inbox members through the
  existing organize path, reports stable per-file categories without absolute paths,
  skips already moved/missing/not-ready members, and is safe to replay. A partial or
  retryable result persists `retry-pending`, offers a persistent `Retry move` action,
  and becomes actionable `ready` only after that card is known delivered and its
  actual anchor is compare-and-swap bound; a later retry moves only remaining Inbox
  members. A fully moved or already-moved result, including a missing-only result,
  persists `result-pending` and is removed only after the terminal card is known
  delivered. Directory buttons carry an opaque digest bound to the directory and the
  displayed order, so a reorder or membership change rejects the old selection as
  stale without mutation. Stale, expired, cross-action, and cross-requester interaction
  tokens are rejected without mutation, callbacks are acknowledged once before any
  library work, and startup never reruns batch organization to render a result.
- Interaction prompts persist an internal bot-message anchor, migrated rows default to
  NULL, and rebinding is a requester-scoped compare-and-swap that must match the
  expected current anchor, including NULL. Text input edits the stored prompt message.
  Every card that retains Back, Cancel, Retry, directory, or pagination controls is
  delivered through one edit-first helper that returns the actual edited or
  replacement message ID and compare-and-swaps the anchor from the expected old ID to
  that ID. If the message is provably uneditable, exactly one replacement message is
  sent and its message ID is bound; a replacement that loses the compare-and-swap is
  marked stale. Timeouts, flood waits, oversized text, ambiguous RPC/network failures,
  and arbitrary exceptions never duplicate the send. Not-modified edits count as
  success; cancellation is re-raised.
- Completion-card actions are edit-first through directory choice, New directory, the
  relative-path prompt, confirmation, working state, and result on the same message
  anchor. Back returns to the chooser; Cancel is available at the chooser, prompt, and
  confirmation. Keep preserves the completion summary and states that the file remains
  in Inbox. Invalid or slash-prefixed folder input stays on the prompt with the
  relative-path example and creates no job.
- Only one active text workflow exists per actor. A single `BEGIN IMMEDIATE` activation
  verifies the keeper row, actor, expiry, action, and expected step, transitions it to
  the target awaiting/confirmation state, and only then deletes older active text
  workflows in the same transaction without deleting page interactions or ready
  completion buttons. Concurrent activation of two rows leaves exactly one live
  workflow, never zero. Text routing runs before link submission; `/cancel` without a
  token cancels the active prompt, and `/cancel <id>` keeps job cancellation.
- Confirmation work is claimed by atomically transitioning an authorized unexpired
  interaction from `confirm` to `working-confirm` or `confirm-new-directory` to
  `working-confirm-new-directory`; exactly one concurrent callback wins. Duplicate
  callbacks report processing or stale without overwriting the winner's card.
  Recoverable single and batch failures restore the confirmation or ready retry state,
  rebind any replacement Back/Cancel/Retry card, and render its controls. Success
  atomically moves the claimed row to non-actionable `result-pending` and deletes
  sibling completion-action rows for that job. The result text is reconstructed from
  current authorized job and catalog state, and the row is removed only after a known
  delivery plus expected-step and expected-anchor compare-and-delete. Terminal cards
  have no buttons; a replacement send binds its actual anchor with a pending-safe CAS.
  The library mutation and its immediate transition to a pending row form one
  cancellation-safe critical section, including new-directory creation and batch
  organization. An outer cancellation after work begins lets the operation and pending
  write settle, then propagates; a pre-commit failure may still restore the exact prior
  step and prompt anchor, but a committed effect never restores a duplicate action.
  Persistence, delivery, and known-success cleanup are shielded together, an unknown
  client-side cancellation during delivery propagates while the row stays pending, and
  cancellation is never swallowed. A replacement send that returns without a positive
  message identity is ambiguous delivery: it does not consume a terminal row or
  promote a retry row against the old anchor. This is an asynchronous cancellation
  guarantee only; SIGKILL or power loss between filesystem and catalog boundaries
  relies on idempotent reconciliation and replay. One atomic startup recovery
  transaction returns the restored sessions. After Telegram startup and before
  callback handlers accept events, the adapter best-effort edits each anchor into a
  step-bound card:
  confirmations keep pending name/folder context with Confirm, Back, and Cancel;
  `confirm-new-directory` shows Create and move; legacy `working` rows become
  interrupted-action cards with the appropriate Choose folder or Retry move control
  and Cancel. An absent or provably uneditable anchor gets one replacement send bound
  by compare-and-swap. Failures are per-session, log only step, outcome, category, and
  error type, do not abort startup, and do not change job state. If edit and send both
  fail, the restored row stays recoverable through `/files`, `/history`, `/dirs`,
  `/rename`, or `/archive`.
- Pending result and retry rows survive restart and are listed only after storage
  organization reconciliation. The adapter restores them before callback handlers
  start, rechecks current authorization, skips and retains unauthorized rows,
  isolates every failure, and never calls a single-file or batch organization API to
  render a result. There is no fixed backlog cap, so later rows are attempted in the
  same startup even when an earlier row repeatedly fails. Expired pending rows remain
  recoverable; a stranded working claim is restored with a renewed actionable expiry,
  while other expired actionable rows remain stale. An ambiguous or unknown delivery
  failure keeps the row for the next start. Repeated identical moves and repeated
  creation of an already-existing directory do not append duplicate audit rows. The
  schema remains version 4 and this behavior requires no migration.
- `/files` and `/history` commands send one message, while Next and Previous callbacks
  edit the originating bot message. A page callback atomically consumes its exact
  authorized, unexpired interaction only when action and step still match, so the
  first concurrent callback owns it and every loser is stale before creating
  successors or editing. Successors are created only after ownership, initially
  anchored to the callback message. Delivery is shielded and its result is committed
  before cancellation is re-raised, so a same-message edit needs no rebind and a
  visible successor card keeps live tokens. A provably uneditable message produces
  one fallback with valid buttons; every successor anchor moves to the replacement in
  a shielded critical section. If rebind fails, the replacement is disabled, the
  successors are removed, the exact original interaction is restored, and the
  original card is re-rendered (or one retry card is sent and bound) so the restored
  source token is visible. A pre-delivery non-fallback failure, oversized render,
  ambiguous error, or cancellation deletes successors and restores the exact original
  interaction unless it expired during delivery. Not-modified edits produce no
  duplicate; replayed or concurrently consumed page tokens remain stale without
  mutation; acknowledgement stays first and exactly once. Delivery logs contain only
  outcome, delivery, safe error category, and latency.
- Command-menu registration uses the exact eleven commands for the default and
  all-private-chat scopes, runs after bot start and controller construction but before
  startup notices and worker steady state, and isolates each scope failure with logs
  that contain only outcome, scope, and error type.
- Accepted link/upload messages show `Download queued · <short-id>`, source, pending
  status, and the next `/status` guidance; album intake shows the file count, initial
  status, and that Move all appears after all files complete. Detailed completion and
  failure summaries place `Job: <short-id>` immediately after the title without
  changing the remaining field order.
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
- Startup recovery marks eligible interrupted jobs paused, then sends each authorized
  user one best-effort initialization message with version, server-local ISO timestamp
  with timezone, and their paused count. View is requester-scoped; Continue resumes only
  that user's paused jobs, wakes Workers, and is safe to replay. One failed chat does not
  abort startup, ordinary pending jobs proceed, and repeated restarts leave uncontinued
  jobs paused.
- Tests reject traversal, absolute paths, symbolic links, ambiguous short IDs,
  unexpected file types, and overwrite attempts.
- English and Chinese documentation, navigation, examples, and operations guidance are
  synchronized.

## Deployment and rollback

Stop the service and back up the SQLite database, Telegram sessions, and download
root. Rehearse migration and reconciliation against a copy. Report counts and
failures without logging content or private Telegram references.

After deployment, verify schema version 4, file counts, permissions, paused-job
counts, per-user startup messages, View/Continue behavior, active jobs, album intake,
detailed completion messages, the Move all flow, shared commands, in-place pagination,
one-card edit-first flows with Back/Cancel/working, one active text workflow, command-
menu registration, prompt edits, callback recovery, and container stability. Back up
and restore the SQLite
database, sidecars, Telegram sessions, and complete download root as one schema-v4
consistency unit. Rollback means stopping the new version and restoring the matching
database and download-root backups together before starting the prior application.
