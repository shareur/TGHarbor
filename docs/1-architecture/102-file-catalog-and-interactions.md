# 102 — File catalog and interactions

[简体中文](../zh-CN/1-architecture/102-file-catalog-and-interactions.md)

TGHarbor v0.2 separates download execution from post-download organization. A job
describes acquisition and retry state; a stored-file record describes the current
file name, location, and organization history.

## Privacy and metadata

The Telegram Provider may expose normalized message text truncated to 160 Unicode
characters, the original file name, MIME type, and size. TGHarbor does not persist
the complete message body. The bounded title is presentation metadata, not a file
name. Logs must not contain message text, Telegram links, credentials, access hashes,
or raw Telegram objects.

## Persistent model

Schema version 4 keeps bounded display metadata and a reserved Inbox name on jobs,
the batch, attempt, resume, and source-metadata fields introduced in v3, and adds:

- a nullable opaque `batch_id` and `batch_expected_size` with a default of 1;
- nullable `started_at` and `finished_at` attempt timestamps;
- a persistent `resume_required` flag with a default of false;
- nullable `origin_group_id` and `channel_id` stored as text so Telegram ID width
  or format is never assumed;
- a nullable `batch_id` on interaction sessions while the existing job association
  remains the anchor for single-file actions;
- a nullable `prompt_message_id` on interaction sessions that anchors the bot
  message a prompt was rendered in, so later text input can edit that message
  instead of posting another one;
- explicit `working-confirm` and `working-confirm-new-directory` steps that record
  which confirmation a claim consumed, so restart recovery can restore the exact
  prior confirmation instead of guessing; a legacy generic `working` row recovers
  to the safe `ready` retry state;
- explicit non-actionable `result-pending` and `retry-pending` steps that reuse the
  existing interaction rows as a compact durable result-delivery outbox; the schema
  remains version 4 and requires no migration for this behavior;
- a compact per-requester `batch_notifications` row for completion-notice state.

Existing interaction rows migrated from schema v3 receive a NULL prompt anchor.
The anchor is an internal Telegram message identifier and never enters callback
payloads or logs.

It also retains:

- a **stored file** for each completed output, with a root-relative path,
  original/current name, size, MIME type, Inbox/Archive state, and latest operator;
- an **interaction session** containing an authorized actor, action, step, target
  file or batch, pending input, and expiry;
- an **audit event** containing actor, operation, source/destination paths, and time.

Indexes are limited to claim, resumed-job, and batch lookups: the existing claim
index plus partial indexes for `resume_required` and non-null `batch_id`, and an
index for stale notification claims. Raw batch and source identifiers are not
placed in callback payloads.

All authorized users can list and organize the shared catalog. Job status and
cancellation remain requester-scoped. Migrations run one version at a time in
explicit transactions, preserve existing rows, and reject newer schemas. A failed
step rolls back before initialization stops. Rollback after migration requires
restoring the database and download-root backup together.

## Attempt timing and restart continuation

`started_at` is set by the claim that starts an attempt. A retryable failure clears
it while the job waits, and the next claim sets a fresh value. `finished_at` is set
atomically with completion, terminal failure, or cancellation, so elapsed time and
completion notices describe the attempt and terminal transition that actually
occurred. Retry accounting is not reset by restart continuation.

Restart recovery has three outcomes for running jobs. A persisted cancellation
becomes cancelled; an exhausted attempt becomes failed; an eligible interrupted job
becomes pending with `resume_required` set. Paused jobs are not claimable and remain
paused across repeated restarts. A requester can list its paused jobs and continue
all or a selected subset. Continuation is requester-scoped, idempotent, preserves
the attempt count, clears the pause flag, and wakes Workers. Ordinary pending jobs
are unaffected.

Startup also recovers persisted working interactions. `working-confirm` returns to
`confirm`, `working-confirm-new-directory` returns to `confirm-new-directory` while
retaining pending input and the prompt anchor, and a legacy generic `working` row
returns to `ready` with pending input cleared. This recovery deliberately prefers a
safe, repeatable confirmation or retry action over guessing that an interrupted
library operation was destructive; the underlying organization operations are
already idempotent for already-moved or already-archived members.

The recovery transaction is atomic and returns the exact restored sessions, not only
a count, before callback handlers accept events. After the Telegram client and
controller start, the adapter best-effort edits each stored prompt anchor into a card
whose controls match the restored state: confirmations keep pending file/folder
context with Confirm, Back, and Cancel; `confirm-new-directory` uses Create and move;
a legacy `ready` row renders an interrupted-action card with the action-appropriate
Retry or Choose folder control and Cancel. An absent anchor, or an edit that is
provably impossible, triggers exactly one replacement send and a compare-and-swap
bind. Each session is isolated: an edit, send, or repository failure is logged only
as step, outcome, category, and error type without IDs or content, and never aborts
startup or changes job state. If both edit and send fail, the database state remains
restored and the operator resumes through `/files`, `/history`, `/dirs`, `/rename`,
or `/archive`; visible recovery is not claimed as successful.

## Durable result delivery

Committed organization results must survive a failed or cancelled final edit. One
interaction row is the durable outbox entry: `result-pending` holds a committed
single-file result or a fully terminal batch result, and `retry-pending` holds a
partial batch result whose Retry card is not yet known visible. Both steps are
non-actionable, so no callback can consume them, and both ignore actionable expiry,
so an expired but undelivered row remains recoverable. A suspended `working`,
`working-confirm`, or `working-confirm-new-directory` claim is also recovered even
after its original expiry, because it is stranded rather than safely actionable;
restoration installs a renewed actionable expiry from the existing interaction
lifetime. Other expired actionable rows keep their existing stale behavior.

After the library operation reaches a successful or idempotent terminal result, the
adapter atomically transitions the owned working claim to the pending step and, for a
single file, deletes sibling completion-action rows for that job in the same
`BEGIN IMMEDIATE` transaction. The pending row is never deleted before a known
successful final delivery and an expected-step, expected-anchor compare-and-delete.
Terminal cards have no buttons; a retry card becomes actionable `ready` only after it
is known delivered and its actual anchor is compare-and-swap bound, and that
transition installs a fresh actionable expiry. Delivery remains edit-first, with
exactly one replacement send only for the provably uneditable error set. A send that
returns without a positive message identity is treated as ambiguous delivery: it does
not consume a terminal row or promote a retry row against the old anchor. Timeouts,
flood waits, network or RPC ambiguity, unknown failures, and client cancellation do
not fall back and leave the pending row intact.

The library mutation and the immediate transition to `result-pending` or
`retry-pending` form one cancellation-safe critical section, including new-directory
creation and batch organization. Persistence, delivery, and known-success cleanup are
shielded as well. An outer cancellation after work begins lets the operation and the
pending-state write settle, then propagates; cancellation is never swallowed. A
pre-commit failure or cancellation may still restore the prior confirmation, but a
committed effect can never restore a duplicate confirmation. This is an asynchronous
cancellation guarantee only: a SIGKILL or power loss between a filesystem write and
its catalog row relies on idempotent reconciliation and replay, not cross-system
atomicity. Repeated identical file moves and repeated creation of an already-existing
identical directory do not append duplicate audit rows, so recovery replays are safe.

Startup lists every pending row only after storage organization reconciliation and
restores them before callback handlers start. There is no fixed backlog cap: later
rows are attempted even when an earlier row repeatedly fails, and each row failure is
isolated. Each card is reconstructed from current authorized job and catalog state,
never persisted message text or host paths, and no single-file or batch organization
API is rerun. Rows whose actor is no longer authorized are skipped and retained. A
failed or ambiguous edit keeps the pending row for the next start.

## Batch intake and source metadata

Batch identity is supplied explicitly at submission by a trusted adapter and is
opaque to core. The Telegram album handler submits every media member of one album
with a deterministic digest built from a fixed version prefix, requester ID,
configured session/client identity, source chat, and Telegram `grouped_id`. The raw
components are never placed in callbacks or logs. The single-message handler ignores
grouped messages, so an album is submitted once. A direct link to one album member is
not expanded remotely and remains a one-member job.

`origin_group_id` and `channel_id` are optional text metadata. `origin_group_id` is
the original group or channel peer only when Telegram supplies an unambiguous peer;
`channel_id` is set only for an original/source channel peer. Core and the adapter
never derive one from the other, from the requester, or from a private conversation.
Forwarded metadata is used only when every member exposes the same unambiguous peer;
private, unavailable, or ambiguous metadata is stored empty and rendered `N/A`.

A batch is eligible for an "all downloaded" action only when its expected size is
greater than one, exactly that many jobs exist, all are terminal, and every member
completed. Partial failure or cancellation produces no move-all action but remains
queryable through requester-scoped progress counts. The durable notice row is an
outbox with a small state machine:

```text
absent --eligible claim--> claimed --send and sent mark success--> sent (terminal)
claimed --send failure or mark failure release--> pending --claim--> claimed
claimed --stale claim recovery or startup release--> pending or a replacement claim
```

The eligibility check and claim run in one transaction, so concurrent Workers and the
dispatcher cannot both own one notice. Sent notices are terminal and are never
redriven. A failed send releases its claim. The dispatcher lists a bounded page of
eligible batches whose notice is absent, pending, or held by a stale claim, and
retries them on a fixed poll while the service runs. Job completion wakes the same
dispatcher. Startup recovery releases every persisted claim because the single
instance lock proves no earlier sender is alive, and the service then performs an
immediate redrive. A later claim can also take over a stale claim without a restart.

Each delivery attempt calls an idempotent ensure step that creates or refreshes exactly
one persistent interaction for the requester and opaque batch ID, sends one
`Move all to new folder` message with that interaction, and marks the notice sent only
after delivery. The callback carries only the interaction ID and a step-bound action
token, so replayed buttons remain resolvable and idempotent.

A persisted interaction is never treated as evidence that Telegram accepted a message:
a crash after interaction creation and before send is redriven as a real send. If a
send fails, the interaction is retained for reuse and the claim is released. If the
send succeeds but the sent-state mark fails, the adapter releases the claim and a
later retry may deliver the chat notice again, but it reuses the same interaction and
callback identity and eventually reaches `sent`. This is deliberate at-least-once
delivery. Telegram acceptance and the SQLite commit are separate systems, so a timeout
after actual delivery or a crash between the two steps makes an occasional duplicate
unavoidable; the implementation never claims exactly-once delivery across that
external boundary.

Moving a batch is Inbox-only. `LibraryAPI.organize_batch(actor_id, batch_id,
target_directory)` moves only the current Inbox-resident successful members of the
actor's batch. It reuses the existing organize path for path validation,
interrupted-move recovery, collision suffixes, audit records, and catalog updates.
The operation is per-file and non-atomic across members. Already archived members are
skipped. Successful jobs with no catalog row are reported as missing; non-successful
members are reported as not ready; storage or catalog failures are reported per file
without aborting other files. Replays are idempotent. Results expose moved, skipped,
failed, already-moved, missing, and not-ready counts and stable categories, never
absolute filesystem paths. Another requester cannot look up or move the batch. The
Telegram adapter calls only this API and never reads paths, storage, or SQLite directly.

A result with failed or not-ready members is retryable: the claimed row moves to
`retry-pending`, the reply offers a persistent `Retry move` action, and only after
that card is known delivered and its actual anchor is compare-and-swap bound does the
row become actionable `ready`. A later attempt can move the remaining Inbox members
while already archived members are skipped. Missing members alone are terminal
because no catalog row exists to retry. A fully moved or already-moved result,
including a missing-only result, moves to `result-pending` and is removed only after
the terminal card is known delivered. Any replayed confirmation or retry token is
then rejected as stale without mutation. Restart reconstructs the card from the
catalog, never reruns the batch operation, and a retry affects only remaining Inbox
members.

## Storage layout

```text
downloads/
├── inbox/       completed files awaiting organization
├── archive/     authorized-user managed directories
└── .partial/    hidden, per-job temporary files
```

The Telegram Provider resolves a deterministic media name before storage: an
explicit job destination name, then a non-empty Telegram file name, then
`telegram_<message-id>` plus a validated extension. The extension must be a short
lowercase ASCII alphanumeric suffix. Otherwise a conservative explicit MIME map
(JPEG, PNG, WebP, and MP4) supplies the extension, and unknown or unsafe metadata
falls back to `.bin`. Storage remains authoritative for final filename and path
sanitization; MIME inference stays in the Telegram adapter.

Desired names are sanitized. A conflict appends a short job identifier before the
extension; unrelated files are never overwritten. All paths remain below the root.
Absolute paths, traversal, empty segments, symbolic-link components, and unexpected
file types are rejected. Completion and organization use same-filesystem atomic
moves and directory synchronization.

The Worker resolves and persists metadata, reserves an Inbox name, downloads to the
job's hidden partial path, atomically commits to Inbox, creates the catalog record,
then publishes actions. Rename/archive operations persist intent before moving; on
restart, recovery finalizes a provable result or reports a conflict without deleting
content.

Existing v0.1 UUID directories are legacy outputs. Reconciliation only handles
completed jobs with tracked regular files, reserves collision-safe names, moves one
file at a time, and leaves ambiguous or failed items unchanged for operator review.

## Bot interactions and queries

Completion actions are `keep`, `rename`, `archive`, and `rename-and-archive`. A
successful download also renders `Download Completed`, the short job ID as `Job`, the
final catalog file name, a logical `/download/<relative parent>` folder, stored size in
MB, persisted start/end times as server-local `HH:MM:SS`, elapsed seconds, average
KB/s, requester ID, and origin group/channel or `N/A`. Query failures fall back to
bounded job data without
changing the completed job. A successful multi-member album can produce one persistent
`Move all to new folder` interaction; single jobs never show it. Directory selection,
new-directory creation, confirmation, cancellation, and pagination are persistent
interaction steps. A prompt that expects text persists the bot message it was rendered
in as its prompt anchor. Callback data carries only an opaque interaction ID and action
token; the batch ID and prompt message ID live only in the interaction row. Every
callback rechecks authorization, expiry, actor ownership, and catalog state.

A directory button carries an opaque short digest bound to both the rendered directory
and the displayed ordering instead of a list index. The adapter re-fetches directories
through the Library API and resolves the digest against the current list. If the order
or membership changed, the callback is rejected as stale without changing the
interaction or the library, and the reply shows refreshed choices. A rendered button
can therefore never resolve to a different directory after a reorder, and payloads stay
within Telegram's 64-byte limit without including a directory name or path.

Every callback is acknowledged immediately and exactly once with a short or empty
spinner answer before catalog, library, or filesystem work begins. That answer is
never the primary feedback. Prompts, successes, stale or expired selections,
authorization and domain errors, and unexpected failures all produce persistent chat
feedback. Where a bot message already carries the interaction, delivery is edit-first
through the underlying client `edit_message` API using the callback chat and message
identity. The adapter never uses Telethon's `CallbackQuery.Event.edit` or `respond`
shorthands after an explicit answer because Telethon 1.45 implicitly answers from
both. A "message not modified" result counts as success. Only errors that establish
the message cannot be edited (invalid or missing ID, author required, write-forbidden,
or edit-expired) fall back to exactly one replacement message. Every workflow card that
retains Back, Cancel, Retry, directory, or pagination controls is delivered through one
helper that returns the actual edited or replacement message ID and compare-and-swaps
the anchor from the expected old message ID to that delivered ID. A replacement that
loses the compare-and-swap has its controls disabled or is marked stale. Timeouts, flood
waits, oversized messages, ambiguous RPC or network errors, and arbitrary exceptions do
not duplicate the feedback: they return one generic safe delivery failure and leave the
interaction recoverable. Text lengths are validated before an oversized edit or send is
attempted. Unexpected failures log only outcome, delivery category, error type, and
latency without actor IDs, interaction IDs, message IDs, callback payloads, names,
paths, message text, or Telegram IDs.

The same one-card anchor covers the directory chooser, New directory, confirmation,
working state, success, partial failure, and cancellation. The chooser includes a
context header, the next action, existing directories in recent-use order, New
directory, and Cancel, and states when no directories exist. Confirmation offers
`Move here` for an existing directory or `Create and move` for a new one, plus `Back`
and `Cancel`. The card shows a visible working state before filesystem or library work
and is then edited to the result. Keep preserves the completion summary and states that
the file remains in Inbox.

The Bot registers a best-effort command menu for the default and all-private-chat
scopes with the exact commands `start`, `files`, `status`, `history`, `dirs`, `cancel`,
`stats`, `rename`, `archive`, `mkdir`, and `help`. Each scope is failure-isolated, logs
only outcome, scope, and error type, and runs after startup before notices and worker
steady state.

`/files` and `/history` share content and button builders. Invoking the command sends
one new message, while a page callback edits the originating bot message in place. A
page callback first atomically consumes and returns its exact authorized, unexpired
interaction only when the action and page step still match; the first concurrent
callback owns the token and every other callback is stale before it can create
successors or edit. Successor interactions are created only after ownership, with the
callback message as their initial prompt anchor, so an edit-first delivery on that same
message needs no rebind. Delivery is completed under a cancellation shield before a
pending cancellation is re-raised, so a card that becomes visible never loses its
buttons to rollback. If an edit error establishes that the message is uneditable,
exactly one replacement message carries freshly valid previous/next buttons and all
successor anchors move from the original message to the replacement inside a shielded
critical section. If that rebind fails, the replacement is disabled, the successor rows
are removed, the exact original interaction is restored, and the original card is
re-rendered (or one retry card is sent and bound) so the restored source token is
actually present in a visible card. If the original expired during delivery, the
original card is disabled instead. Rollback to the original interaction therefore
happens only when no usable successor card remains visible. On a pre-delivery
non-fallback failure, oversized render, ambiguous error, or cancellation, successors
are deleted and the original interaction is restored exactly. An unchanged edit
produces no duplicate, and a replayed or concurrently consumed page token is stale and
changes nothing.

Text routing is deterministic. `/start`, `/help`, and recognized commands remain
available; `/cancel` without a job token cancels the active text prompt, while
`/cancel <job>` keeps job cancellation. Any other text, including unknown
slash-prefixed text, continues the active text workflow before link or upload
submission. Folder prompts validate a relative path; absolute or slash-prefixed input
stays on the same prompt with the relative-path example and never creates a job. Text
sent while a confirmation is pending re-renders that confirmation instead of
submitting a new download.

One active text workflow exists per actor. Activation is one `BEGIN IMMEDIATE`
repository transaction that verifies the keeper row exists, belongs to the actor, is
unexpired, matches the expected source action and step, and transitions to the
requested awaiting or confirmation state. Only then does it delete other active
rename, archive, or batch-move workflows for that actor in the same transaction. Page
interactions and ready completion buttons for other jobs are never deleted, so text
routing stays unambiguous without disabling unrelated cards. Concurrent activation of
two rows leaves exactly one live workflow, never zero. Confirmation work is claimed by
atomically transitioning an authorized unexpired interaction from a confirmation step
to `working`; only the winner performs library or filesystem work. Duplicate callbacks
report that the action is already processing without replacing the winner's card.
Recoverable failures restore the confirmation or ready state and render Back/Cancel or
Retry/Cancel controls; success moves the claimed row to `result-pending` and removes
it only after the terminal card is known delivered. Workflow callbacks whose
message ID no longer matches the stored prompt anchor are stale and mutate nothing. An
invalid payload, a replayed consumed token, or a step-mismatched token returns one-off
feedback without editing a newer card; expired or unauthorized feedback on a live
completion card edits that card.

After restart recovery and Telegram startup, the Bot sends each authorized user one
best-effort initialization message with the package version, a server-local ISO
timestamp with timezone, and that user's paused count. A positive count creates
persistent View and Continue interactions; zero states that no interrupted tasks
exist and omits them. View is requester-scoped and exposes no host paths or other
users' jobs. Continue calls `JobAPI.continue_paused()` for that requester, clears only
their paused jobs, wakes Workers, and reports the resumed count. Replays are
idempotent, cross-user callbacks are denied, and repeated restarts leave uncontinued
jobs paused.

Callback tokens are bound to the interaction's action and current step. A token that
does not match the current step, including a replayed completion action after name or
directory input, is rejected as stale before any state changes, so pending input is
preserved. "New directory" is an explicit confirmation step: the typed path is stored,
shown as a directory that will be created, and confirmation creates the archive
directory through the Library API before organizing the file. Creating a directory
that already exists is idempotent. Expected library failures map to stable messages
for an invalid name or directory, a missing catalog file, or changed library state;
raw exception text, including paths, is never sent to the chat or logged.

Directory lists and archive-selection buttons contain only directories that still
exist under the archive root. They are ordered by most recent use, derived from
existing audit and stored-file timestamps, including directory-creation audit rows
for empty directories. Recency ties and directories without usage history fall
back to case-insensitive alphabetical order. The Library API combines storage
existence with repository recency; the Telegram adapter never queries SQLite or
the filesystem directly.

- `/status` lists requester-owned pending/running jobs.
- `/history` lists requester-owned terminal jobs with filters and pagination.
- `/files`, `/dirs`, and `/stats` read the shared catalog.
- `/rename`, `/archive`, and `/mkdir` mutate the shared catalog for an authorized
  user and record that user's Telegram ID.

Short job identifiers are accepted only when they uniquely resolve inside the
command's authorization scope.
