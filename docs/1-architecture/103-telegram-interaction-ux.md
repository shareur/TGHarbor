# 103 — Telegram interaction UX

[简体中文](../zh-CN/1-architecture/103-telegram-interaction-ux.md)

This document defines the integrated Telegram interaction contract: a
one-card, edit-first workflow for completion actions, its single-send fallback,
message and button templates, the best-effort command menu, the one-active-text-
workflow rule, routing precedence, privacy boundaries, and acceptance checks. It
complements [102 — File catalog and interactions](102-file-catalog-and-interactions.md).

## One-card edit-first UX

The bot message that carries a completion card is the anchor for the whole
organization workflow:

- Keep, Rename, Archive, Rename + archive, and batch Move all act on that card.
- Directory choice, New directory, the relative-path prompt, confirmation,
  working state, success, partial failure, cancellation, and stale feedback replace
  that same bot message instead of appending a chain of new messages.
- Every callback is acknowledged first and exactly once with a short or empty
  spinner answer. That answer is never the primary feedback.
- Card edits and replacement sends use the underlying client APIs, never Telethon's
  `CallbackQuery.Event.edit` or `respond`, which implicitly answer again in
  Telethon 1.45.
- A text prompt persists the bot message it is rendered in as
  `prompt_message_id`; typed input edits that message and rebinds the anchor through
  a compare-and-swap that requires the expected current anchor, including NULL.
- A concise context header stays visible through every step: `Job <short-id> · <file>`
  for a single file or `Album · <count> files` for a batch. The header never contains
  a host path.

Each transition follows the same shape:

```text
completion card -> chooser -> prompt / confirmation -> working -> result
```

The Keep action preserves the detailed completion summary and appends
`Kept in Inbox. The file remains in Inbox.` so the card still reflects the file.

## Fallback and error policy

- The adapter edits the originating message through client `edit_message` first and
  treats Telegram's "message not modified" result as success.
- Only edit errors that establish the message cannot be edited (invalid or missing
  ID, author required, write-forbidden, or edit-expired) produce exactly one
  replacement message with the same content and buttons. Every card that retains
  Back, Cancel, Retry, directory, or pagination controls is delivered through one
  helper that receives the actual edited or replacement message ID and
  compare-and-swaps the anchor from the expected old message ID to that delivered ID.
- Timeouts, flood waits, oversized text, ambiguous RPC or network failures, and
  arbitrary exceptions never send a duplicate. They return one generic safe delivery
  failure and leave the interaction recoverable.
- Text length is validated before an oversized edit or send; `MessageTooLong` never
  triggers the same oversized replacement.
- A page callback atomically consumes its exact authorized, unexpired interaction
  only when the action and step still match. The first concurrent callback owns the
  token; all others are stale before creating successors or editing. Successor
  interactions are created only after ownership, with the callback message as their
  initial prompt anchor. An edit on that same message therefore commits without any
  rebind.
- Delivery runs under a cancellation shield and its actual result is examined before
  a pending cancellation is re-raised, so a visibly delivered successor card never
  loses its buttons to rollback. A fallback replacement send moves every successor
  anchor from the original message to the replacement in a shielded critical section
  before cancellation propagates.
- If that replacement rebind fails, the replacement is disabled, the successor rows
  are removed, the exact original interaction is restored, and the original card is
  re-rendered (or one retry card is sent and bound) so the restored source token is
  present in a visible card. If the original expired during delivery, the original
  card is disabled instead. Successors are rolled back to the source only when no
  usable successor card remains visible, so a visible card never points at deleted
  tokens and a restored token is never absent from the visible card.
- On a pre-delivery non-fallback edit failure, oversized render, ambiguous error, or
  cancellation, successors are deleted and the exact original interaction is restored
  with its ID, actor, action, step, expiry, pending data, and prompt anchor, unless it
  expired during delivery.
- A replacement message that loses the prompt-anchor compare-and-swap has its buttons
  disabled or is marked stale without touching the live card.
- Cancellation is never swallowed. Once a confirmation claims working, the library
  mutation and its immediate transition to `result-pending` or `retry-pending` run as
  one cancellation-safe critical section, including new-directory creation and batch
  organization. An outer cancellation after work begins lets the operation and the
  pending-state write settle, then propagates. A pre-commit failure before the
  library operation commits may still restore the prior confirmation or ready retry
  state and prompt anchor, but a committed effect never restores a duplicate action.
- Final result delivery and its known-success cleanup are shielded together. Only a
  known edit success or one provably-uneditable replacement send may remove a
  terminal pending row or make a retry pending row actionable `ready`. A send return
  without a positive message identity is ambiguous delivery: it neither consumes a
  terminal row nor promotes a retry row against the old anchor. Ambiguous failures and
  unknown client-side cancellation retain the pending row for restart recovery.
- Expired or unauthorized feedback on a live completion card edits that card.
- An unparseable callback payload, a replayed consumed token, a token that does not
  match the current step, or a callback from a message ID other than the stored
  prompt anchor returns one-off feedback without mutating the interaction. This
  preserves a newer card and any live retry controls.
- Raw exceptions, host paths, interaction IDs, message IDs, callback payloads,
  names, and paths never appear in chat text or logs. Logs contain only outcome,
  delivery mode, error type or safe category, and latency.

## Message and button templates

Accepted messages:

```text
Download queued · <short-id>
Source: Bot upload | Telegram message link
Status: pending
Next: /status <short-id>
```

```text
Album queued · <count> files
Status: pending
Move all appears after all files complete.
```

Directory chooser:

```text
Job <short-id> · <file>

Choose where to move this file.
Existing directories, most recently used first:
• videos
• 2025
Send a relative path as text, or use the buttons below.
[Existing directory] [New directory] [Cancel]
```

When no directory exists, the existing-directory list is replaced by
`No archive directories exist yet.` Batch choosers use
`Choose where to move all files in this album.`

New-directory prompt:

```text
Job <short-id> · <file>

Send a relative path, for example: videos/2026. Do not start with /.
[Cancel]
```

Invalid folder input keeps the same step and edits the same prompt:

```text
Job <short-id> · <file>

That is not a valid relative directory. Send a relative path, for example: videos/2026. Do not start with /.
[Cancel]
```

Confirmation for an existing directory:

```text
Job <short-id> · <file>

Confirm file organization:
Name: <name>
Directory: <directory>
Next: Move here moves the file into this directory.
[Move here] [Back] [Cancel]
```

Confirmation for a new directory uses `Directory: <directory> (will be created)`,
`Next: Create and move creates the folder and moves the file.`, and
`[Create and move] [Back] [Cancel]`. Batch confirmations use
`Confirm batch move:` and the same button pattern.

Working and result states:

```text
<header>

Working… Moving the file.
```

```text
<header>

Completed:
[<short-id>] <name> · <size>
<relative-path>
```

Batch results keep the existing `Batch move result:` counts. A result with failed or
not-ready members appends `Some files were not moved. Choose a directory again to
retry.` and a persistent `Retry move` button. Fully moved or already-moved results
consume the workflow. Failures never contain host paths or raw exception text.

## Text routing precedence

An authorized private-chat text update is routed in this order:

1. Album handling consumes a grouped album exactly once.
2. `/start` and `/help` remain global.
3. `/cancel` without a job token cancels the active text workflow when one exists;
   otherwise it keeps the existing usage reply. `/cancel <job>` keeps its job
   cancellation behavior.
4. Other recognized commands (`/status`, `/history`, `/files`, `/dirs`, `/stats`,
   `/rename`, `/archive`, `/mkdir`) run normally.
5. Any other text, including unknown slash-prefixed text, belongs to the active text
   workflow before it can be interpreted as a link or upload.
6. Only when no text workflow is active is the text submitted as a Telegram link or
   Bot media.

At `await-directory` and `await-new-directory`, input is validated as a relative
archive path. Absolute paths, slash-prefixed values, backslashes, empty segments, and
traversal components keep the awaiting step, edit the same prompt, and never create a
job or start a download. At `await-name`, text is treated as the pending name. At a
confirmation step, text re-renders that confirmation instead of submitting a
download.

## One active text workflow and WORKING claim

The repository exposes schema-free operations:

```text
activate_text_workflow(interaction_id, actor_id, expected_action,
                       expected_steps, target_step, ...) -> session | None
claim_working_interaction(interaction_id, actor_id,
                          expected_action, expected_steps) -> session | None
restore_working_interaction(interaction_id, actor_id, target_step, ...) -> session | None
```

Activation runs as one `BEGIN IMMEDIATE` transaction. It verifies that the keeper row
exists, belongs to the actor, is unexpired, matches the expected action and source
step, and transitions to the requested awaiting or confirmation state. Only then does
it delete older text workflows for the actor whose action is `rename`, `archive`,
`rename-and-archive`, or `batch-move` and whose step is `await-name`,
`await-directory`, `await-new-directory`, `confirm`, or `confirm-new-directory`.
Page interactions and ready completion buttons, including unrelated jobs, are never
touched. Concurrent activation of two rows leaves exactly one live workflow, never
zero, and `latest_interaction_for_actor()` selects it deterministically.

Before single-file or batch library/filesystem work, the controller atomically claims
the confirmation by transitioning `confirm` to `working-confirm` or
`confirm-new-directory` to `working-confirm-new-directory`. Only one concurrent caller
wins. Losers report `This action is already being processed.` or stale feedback without
editing the winner's card or overwriting its result. Before the library or filesystem
data commits, a recoverable failure or cancellation restores the original confirmation
or a ready retry state, rebinds any replacement Back/Cancel/Retry card, and renders its
controls. Once organization commits, the claimed row atomically transitions to the
non-actionable `result-pending` or `retry-pending` state and can no longer be restored
as a confirmation. Final result delivery is then reconstructed from current authorized
state and retried without rerunning organization. A terminal pending row is consumed
only after a known successful delivery and the expected anchor and state settlement;
`retry-pending` becomes actionable `ready` only after its actual message anchor is
bound, with a fresh actionable expiry. An unknown or ambiguous delivery failure retains
the pending state for the next start. An outer asynchronous cancellation after work
begins is settled and then re-raised; SIGKILL or power loss between filesystem and
catalog boundaries is not claimed to be cross-system atomic and relies on idempotent
reconciliation and replay.

A hard process stop is handled at startup. One atomic recovery transaction returns the
restored sessions: persisted `working-confirm` and
`working-confirm-new-directory` rows return to their exact prior confirmation with
pending input and anchor retained, and a legacy generic `working` row returns to
`ready` with pending input cleared so no destructive action is guessed. After the
Telegram client and controller start, and before callback handlers accept events, the
adapter best-effort edits each recovered anchor into a safe card. Confirmations show
the pending name and directory with `[Move here] [Back] [Cancel]`, or
`[Create and move] [Back] [Cancel]` for a new directory. A recovered `ready` row shows
an interrupted-action card with the action-appropriate `Choose folder` or `Retry move`
control and `Cancel`. If no anchor exists or the edit is provably impossible, exactly
one replacement is sent and bound by compare-and-swap. Sessions whose actor is no
longer authorized are skipped without a chat message. Each session is isolated:
failures log only step, outcome, category, and error type, never IDs, names, or paths,
and never abort startup or change job state. If both edit and send fail, the database
row stays restored and the operator can continue with `/files`, `/history`, `/dirs`,
`/rename`, or `/archive`; the service does not claim visible recovery succeeded.
Persisted working state therefore never strands the interaction permanently.

## Durable result delivery

A committed organization result is kept in the same interaction row as a compact
outbox entry, with no schema change beyond version 4:

```text
working claim --library result committed--> result-pending --known delivery + CAS--> removed
working claim --partial batch result------> retry-pending --known delivery + anchor CAS--> ready
```

`result-pending` holds a terminal single-file or fully terminal batch result and is
non-actionable. `retry-pending` holds a partial batch result whose Retry card is not
yet known visible and is also non-actionable until it settles. Both ignore actionable
expiry, so an expired but undelivered result stays recoverable. A stranded `working`,
`working-confirm`, or `working-confirm-new-directory` claim is recovered even after
its original expiry and restored with a renewed actionable lifetime; other expired
actionable rows keep their stale behavior. For a single file the transition also
deletes sibling completion-action rows for the job atomically.

The terminal text is reconstructed from current authorized job and catalog state on
both live delivery and restart; persisted message text and host paths are never used.
Delivery stays edit-first and uses one replacement send only for the provably
uneditable error set. A replacement binds its actual anchor by expected-state compare
and swap before cleanup. Terminal cards have no buttons. A Retry card becomes
actionable `ready` only after it is known delivered and its actual anchor is bound,
and that transition installs a fresh actionable expiry, so a callback cannot consume
a card that is not visible or that is already expired.

The library mutation and the pending transition form one cancellation-safe critical
section, and persistence, delivery, and known-success cleanup are shielded as well.
An outer cancellation after work begins settles the operation and pending write before
propagating; a pre-commit failure may still restore the prior confirmation, while a
committed effect can never restore a duplicate confirmation. This is an asynchronous
cancellation guarantee only: SIGKILL or power loss between filesystem and catalog
boundaries relies on idempotent reconciliation and replay. Repeated identical moves
and repeated creation of an already-existing directory do not duplicate audit rows.

At startup, storage organization reconciliation runs first, then working claims are
recovered with a renewed actionable lifetime, and all pending result and retry rows
are listed and restored before callback handlers start. There is no fixed backlog cap,
so later rows are attempted even if an earlier row fails; unauthorized rows are
skipped and retained, every row failure is isolated, and recovery only reads job and
catalog state. A failed or ambiguous edit keeps the row for the next start.

## Command menu

`register_commands()` is best effort and uses Telethon
`SetBotCommandsRequest` with `BotCommand` entries. It registers the default scope and
the all-private-chat scope (`BotCommandScopeDefault` and `BotCommandScopeUsers` in
Telethon). The exact command set and English descriptions are:

| Command | Description |
| --- | --- |
| `start` | Show help and usage |
| `files` | List shared files |
| `status` | Show active jobs or one job |
| `history` | Show completed, failed, or cancelled jobs |
| `dirs` | List archive directories |
| `cancel` | Cancel a job or the active prompt |
| `stats` | Show library statistics |
| `rename` | Rename a stored file |
| `archive` | Move a file into an archive directory |
| `mkdir` | Create an archive directory |
| `help` | Show help and usage |

Each scope is failure-isolated. A scope failure logs only outcome, scope, and error
type; it does not abort startup, and typed commands remain fully usable. Registration
runs after the bot client starts and the controller is constructed, before startup
notices and worker steady state. Per-authorized-user scopes are not registered because
Telethon requires peer/user entities for them and the optional benefit does not
justify the extra failure surface.

## Privacy

- Chat feedback uses logical library paths and bounded names only.
- Prompt anchors, callback payloads, interaction IDs, Telegram IDs, requester IDs,
  and message IDs are never logged.
- Directory buttons carry an opaque digest bound to the displayed directory and
  ordering; a changed list rejects the old selection without mutation.
- Cross-user, cross-action, expired, and replayed tokens are rejected without
  library or filesystem mutation.

## Acceptance

- Single-file and batch completion cards route through chooser, New directory,
  typed valid and invalid input, confirmation, working, and result on one message
  anchor.
- Every retained Back, Cancel, Retry, directory, or pagination card that falls back
  to a replacement message rebinds the interaction anchor to the replacement ID;
  clicking Back or Retry on the replacement succeeds while the old anchor is stale.
- Back returns from confirmation to the chooser; Cancel works at chooser, prompt,
  and confirmation.
- Invalid or slash-prefixed folder input stays in the awaiting step, shows the
  relative-path example, and creates no job.
- Keep preserves the completion summary and states that the file remains in Inbox.
- Accepted single uploads and links show source, pending status, and next `/status`
  guidance. Album intake shows the file count, the initial status, and that Move all
  appears after all files complete.
- Detailed completion and failure summaries place `Job: <short-id>` immediately
  after the title without changing the remaining field order.
- Partial batch results persist `retry-pending`, keep retry controls on the same card,
  and become actionable `ready` only after the card is known delivered and its actual
  anchor is bound. Terminal results persist `result-pending` and are removed only after
  a known delivery. Replay, cross-action, and cross-user tokens remain stale without
  mutation, and a retry moves only remaining Inbox members.
- Concurrent page callbacks have exactly one owner: one edit, one successor set,
  and stale or no mutation for every loser. Successors start anchored to the callback
  message, same-message edits need no rebind, and fallback replacements rebind inside
  a shielded critical section. Rebind failure disables the replacement and re-renders
  the restored source token. Every visible page token resolves to a persisted live
  row; rollback happens only when no usable successor card is visible.
- Cancellation after a working claim never strands the interaction: pre-operation
  cancellation restores the prior confirmation and anchor, post-commit cancellation
  preserves a pending result, and startup recovery restores any persisted working or
  pending state. Recovered confirmations and legacy ready cards are edited into
  visible, step-bound controls; absent anchors and uneditable messages use one bound
  replacement, and failures stay isolated and recoverable by commands.
- A failed or cancelled final edit or send leaves a recoverable result row; restart
  delivers it from current catalog state without rerunning organization, and an
  ambiguous failure keeps it for the next start.
- Only one active text workflow exists per actor, and starting a new one leaves
  pages and ready completion buttons intact.
- Command registration uses the exact set and two scopes, is startup-ordered, and
  isolates scope failures without leaking IDs.

## Related documents

- [102 — File catalog and interactions](102-file-catalog-and-interactions.md)
- [002 — Feature overview](../0-foundation/002-feature-overview.md)
- [003 — Bot command reference](../0-foundation/003-command-reference.md)
- [202 — v0.2 Library workflow](../2-implementation/202-v0-2-library-workflow.md)
- [301 — Core Downloader operations](../3-operations/301-core-downloader-operations.md)
