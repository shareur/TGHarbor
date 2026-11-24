# 301 — Core Downloader operations

[简体中文](../zh-CN/3-operations/301-core-downloader-operations.md)

## Prerequisites

Create a Telegram application to obtain an API ID and API hash, and create a Bot
token through BotFather. Determine each allowed user's numeric Telegram ID. Never
commit these values, `.env`, `config.yaml`, session files, the database, or downloaded
content.

Copy and edit the examples:

```bash
cp config.example.yaml config.yaml
cp .env.example .env
cp compose.example.yaml compose.yaml
```

For a local Python process, put values in the ignored `config.yaml` or export the
documented environment variables. Compose loads the ignored `.env` automatically.
The local `compose.yaml`, `config.yaml`, and `.env` files are intentionally ignored;
only their sanitized examples belong in Git.

## Local startup

Install the project as described in [Development setup](../0-foundation/001-development-setup.md),
then authorize and start it:

```bash
tgharbor login --config config.yaml
tgharbor run --config config.yaml
```

Run login only in a trusted interactive terminal. The verification code and
two-step password are passed directly to Telethon and are not sent through the Bot.
This command authorizes the separate user-account client for submitted message
links; it does not log in the Bot. The Bot itself uses the BotFather token. The
phone setting is optional because the login command can prompt for it, and the
resulting session is persisted under the configured sessions path.

## Docker startup

The example uses project-scoped Docker-managed volumes named `tgharbor_data` and
`tgharbor_downloads`. It contains no fixed host storage path and runs as the non-root
user built into the image. It pins the local image name to `tgharbor:0.2.3` rather
than a floating minor tag. The user session must exist before unattended startup:

```bash
chmod 600 .env config.yaml
docker compose build --pull
docker compose run --rm tgharbor login
docker compose up -d
docker compose logs -f tgharbor
```

The Compose build uses OCI provenance defaults that identify an uncommitted local
build. A release image must instead be built from an immutable candidate with the
revision, source, and created arguments required by the
[release-security procedure](302-release-security.md#container-identity-and-provenance).
The local `tgharbor:0.2.3` tag is not an immutable deployment identity; verify and
record the release image digest before any rollout.

Compose maps `config.yaml` read-only. The database and both Telegram sessions live in
the data volume; completed media and organization state live in the downloads volume.
Recreating the container preserves both volumes, while `docker compose down -v`
deletes them and must not be used unless their contents are intentionally disposable
or already backed up. Do not scale the service above one replica: TGHarbor deliberately
uses one application-instance lock and multiple Workers inside that process.

### Optional host bind mounts

Operators who require visible host paths can edit their ignored `compose.yaml` and
replace the two named-volume mounts with bind mounts:

```yaml
    volumes:
      - ./config.yaml:/config/config.yaml:ro
      - /srv/tgharbor/data:/data
      - /srv/tgharbor/downloads:/downloads
```

Create those directories for the image user before startup:

```bash
sudo install -d -o 10001 -g 10001 -m 0750 \
  /srv/tgharbor/data \
  /srv/tgharbor/downloads
```

If an operator explicitly overrides the container user, both bind sources must use
that same UID/GID. Keep host-specific paths and identity overrides only in the ignored
local Compose file, never in `compose.example.yaml`.

## Logging

`logging.level` controls TGHarbor's own log records. It does not enable the Telethon
library's lower-severity output: the Telethon logger namespace is always limited to
`ERROR` or stricter, even when `logging.level` is `INFO` or `DEBUG`. This keeps
high-volume update-difference messages from dominating the log. Telethon writes those
error records itself, so this setting controls the level, not the content Telethon
chooses to emit.

TGHarbor writes logs to standard error; Docker captures both standard output and
standard error through its log driver. Observe them with
`docker compose logs -f tgharbor`, or through the service manager's log output for a
local Python process. Startup reports the worker count, and normal operation does not
repeat tracebacks.

When SQLite or its storage adapter fails, TGHarbor logs a sanitized persistence
diagnostic instead of the raw exception. The record identifies the affected job or
worker and includes a fixed category, the exception class name, and a numeric SQLite
primary code or operating-system `errno` when available. Categories are `busy`,
`open`, `io`, `disk-full`, `permission`, `read-only`, `corrupt`, `schema`, `integrity`,
`adapter`, and `database`. These persistence diagnostics never include SQL text, bound
parameters, row values, filesystem or session paths, Telegram links or identifiers,
filenames, titles, tokens, or the raw exception message.

A transient persistence failure while processing a job (`busy`, `io`, `disk-full`,
`permission`, `read-only`, or `open`) requeues that job under the existing attempt
limit and backoff policy with a bounded reason of the form `database <category>`;
exhausted jobs fail as usual. A permanent category (`corrupt`, `schema`, `integrity`,
`adapter`, or the `database` fallback) fails the job instead of repeating an operation
that cannot recover on its own. A failure while sending a completion notification is
logged but does not change the already persisted result. A failure while claiming work
is logged and the worker keeps polling, so a short-lived lock does not stop the worker;
repeated identical permanent failures are logged once until a different failure or a
success is observed. A repeated transient failure with the same category, exception
class, and code is logged in full on its first occurrence, then at most once per minute
with a suppressed count, and one recovery record follows the next successful iteration.
If recording the final state fails because the database itself is unusable, the job
stays in its running state until startup recovery pauses it for explicit requester
continuation, or fails it once its attempt limit is exhausted. The service cannot force
a terminal record into a store that rejects writes, so restore the database and then
review that job with its requester before relying on its result.

Expected Telegram provider failures are stored and shown to requesters only when their
detail exactly matches a fixed message from the built-in provider. Any other provider
exception detail is reduced to the exception class name before it is logged or stored,
so third-party or adapter text is not echoed to a requester.

Connections wait up to 15 seconds for a competing SQLite writer before the operation
is reported as `busy`. On a `busy` or `io` diagnostic, check concurrent instances,
host storage health, and free space; on `permission` or `read-only`, check the data
directory ownership and mount mode; on `corrupt` or `integrity`, stop the service and
restore from a verified backup rather than deleting the database.

## Bot use

- Send or forward one media message in a private conversation with the Bot.
- Send one `https://t.me/<name>/<id>` or `https://t.me/c/<id>/<id>` link.
- Use `/status` for pending/running tasks, `/status <job-id>` for one task, and
  `/history [page]` for terminal history.
- Use `/cancel <job-id>` to cancel pending work or request cancellation of running
  work.
- Use `/files [page]`, `/dirs [page]`, and `/stats` to inspect the shared library.
- Use `/rename`, `/archive`, and `/mkdir`, or the completion-notification buttons, to
  organize files. Every authorized user shares this library; changes audit the actor.

Inline actions acknowledge immediately and exactly once with a spinner and then report
every prompt, success, stale selection, or error as persistent chat feedback, so the
outcome stays in the conversation. Card edits use the underlying client API rather than
Telethon callback shorthands, which would implicitly answer again. Completion-card
actions are one-card and edit-first: Keep, Rename, Archive, Rename + archive, and batch
Move all edit the originating message through the directory chooser, new-directory
prompt, confirmation, visible working state, and result. Only invalid, missing,
not-author, or edit-expired messages fall back to exactly one replacement message.
Every retained Back/Cancel/Retry/directory/pagination card is delivered through one
helper that rebinds the prompt anchor from the expected old message to the actual
edited or replacement message ID. Timeouts, flood waits, oversized text, and ambiguous
errors report one generic delivery failure without duplicating feedback. The chooser
lists existing archive directories in most-recently-used order plus New directory and
Cancel, and states when no directories exist. Confirmation offers `Move here` for an
existing directory or `Create and move` for a new one, with `Back` and `Cancel`. A
single atomic claim transitions `confirm` to `working-confirm` or
`confirm-new-directory` to `working-confirm-new-directory`, so duplicate callbacks
report processing instead of running library work twice. Recoverable single and batch
failures restore confirmation or retry state and render Back/Cancel or Retry/Cancel on
the replacement card. Cancellation after a claim is shielded: the library mutation and
its immediate transition to a pending row form one cancellation-safe critical section,
including new-directory creation and batch organization. An outer cancellation after
work begins lets the operation and pending write settle, then propagates; a pre-commit
failure may restore the prior confirmation or ready retry state and prompt anchor, but
a committed effect never restores a duplicate action. A committed single-file result or
terminal batch result is non-actionable `result-pending`; a partial batch result is
non-actionable `retry-pending` and becomes `ready` only after its Retry card is known
delivered and its actual anchor is compare-and-swap bound, with a renewed actionable
expiry. The terminal row is removed only after a known delivery plus an expected-step
and expected-anchor compare-and-delete. A send return without a positive message
identity is ambiguous delivery and leaves the row pending. This is an asynchronous
cancellation guarantee only; SIGKILL or power loss between filesystem and catalog
boundaries relies on idempotent reconciliation and replay. This pending work is listed
only after storage organization reconciliation and restored before callbacks are
handled; all rows are listed with no fixed backlog cap. Startup restores every
persisted working state, including a claim whose original expiry passed, with a
renewed actionable lifetime, and returns the restored sessions. After Telegram startup
the bot best-effort edits each recovered anchor into the correct visible card:
confirmations keep pending file/folder context with Confirm, Back, and Cancel; a new
directory shows Create and move; a legacy `working` row becomes an interrupted-action
card with the appropriate Choose folder or Retry move control and Cancel. An absent or
provably uneditable anchor gets exactly one replacement send bound by
compare-and-swap. Each session is isolated; a failure cannot abort startup, change job
state, or block another user. Pending rows are rendered from current authorized
catalog state without rerunning organization; unauthorized rows are skipped and
retained, and ambiguous failures keep the row for the next start. Repeated identical
moves and repeated creation of an already-existing directory do not append duplicate
audit rows. Invalid or slash-prefixed folder input stays on the prompt with the
relative-path
example and never creates a job. Sending text while a confirmation is open re-renders
that confirmation instead of starting a new download. Only one text workflow is active
per user; activation is atomic, and starting a new rename or directory workflow leaves
page buttons and other completion cards intact. `/cancel` without a job ID cancels the
active prompt. The first `/files` or `/history` command sends one new list; Next and
Previous edit that same bot message instead of adding another list. A page callback
atomically consumes its exact authorized, unexpired interaction before creating
successors; the first concurrent callback owns it and all others are stale. Successors
start anchored to the callback message, so an in-place edit needs no rebind; delivery
is shielded and committed before cancellation is re-raised. A fallback replacement
rebinds every successor anchor in a shielded critical section. If that rebind fails,
the replacement is disabled, successors are removed, the exact original interaction is
restored, and the original card is re-rendered (or one retry card is sent and bound) so
the restored source token is visible. A pre-delivery non-fallback failure or
cancellation deletes successors and restores the exact original page interaction, so
the old page remains usable unless its original expired during delivery. A button from
an earlier step or an older prompt anchor is reported as stale and does not overwrite
pending input. Page buttons are single-use: a replayed page button is stale and changes
nothing. Prompt anchors are rebound with an expected-value compare-and-swap so an older
card cannot replace a newer one.

The user account must already be able to open linked messages. The service does not
crawl history or bypass Telegram permissions.

## Backup and restore

Treat the complete data and downloads volumes as one restore point. Stop the service,
create both archives through one-off Compose containers, and securely copy the local
configuration files:

```bash
docker compose stop tgharbor
install -d -m 0700 backup
docker compose run --rm --no-deps --user 0:0 --entrypoint tar \
  -v "$PWD/backup:/backup" tgharbor \
  -cpf /backup/data-volume.tar -C /data .
docker compose run --rm --no-deps --user 0:0 --entrypoint tar \
  -v "$PWD/backup:/backup" tgharbor \
  -cpf /backup/downloads-volume.tar -C /downloads .
tar -cpf backup/private-config.tar .env config.yaml compose.yaml
sha256sum backup/*.tar > backup/SHA256SUMS
```

Verify `SHA256SUMS` before restoring. With the service stopped and the intended empty
or replaceable volumes mounted, use the same one-off pattern with `tar -xpf` into
`/data` and `/downloads`, restore the matching private configuration, and start the
service. Never combine the database from one backup with downloads from another.
Bind-mount deployments archive the complete host directories instead, preserving
ownership and permissions. On startup, organization intents and tracked legacy
outputs are recovered before normal Bot handling. Eligible interrupted running jobs
become paused pending jobs that normal claims skip; their requester must list and
continue them through the Job API. Exhausted interrupted jobs become failed and
persisted cancellation requests become cancelled. For schema-1, schema-2, or
schema-3 upgrades, follow the [v0.2 upgrade and rollback guide](303-v0-2-upgrade.md).

After recovery and Telegram startup, the service sends each configured authorized
user one best-effort private initialization message with the package version, a
server-local ISO timestamp with timezone, and that user's interrupted count. When the
count is positive, persistent View interrupted tasks and Continue interrupted tasks
buttons are included. Workers start before these messages, so ordinary pending jobs
are not blocked. A failure to deliver one user's message is logged only as a generic
error type and does not abort startup or affect other users. Repeated restarts leave
uncontinued jobs paused. Never construct these controls manually against SQLite; use
the Bot interactions and Job API.

Recovered interaction cards are separate from the startup notice. Their database state
is already restored and job state is untouched. When both edit and replacement send
fail, the log records only `outcome=not-visible`, the step, a safe category, and the
error type. Continue through a new command or action path instead of editing SQLite:
use `/files` or `/history` to find the item, then `/rename`, `/archive`, `/dirs`, or
`/mkdir` as needed. Do not claim visible recovery succeeded until a card is present.

## Troubleshooting

- **User session is not authorized**: rerun `tgharbor login` or the Compose login
  command in an interactive terminal.
- **Unauthorized**: add the sender's numeric ID to `authorized_users` or
  `TGHARBOR_TELEGRAM_AUTHORIZED_USERS`, then restart.
- **Command menu is missing or incomplete**: command-menu registration is best effort.
  Confirm the Bot started and check for a warning that records only outcome, scope, and
  error type; one scope failing does not disable typed commands. Reconnecting the
  Telegram client or restarting the service retries registration. Do not add user IDs
  to logs while diagnosing.
- **Permission denied under `/data` or `/downloads`**: for default named volumes,
  confirm the service uses the image's built-in user and recreate only a new empty
  volume after diagnosing ownership. For bind mounts, make both host directories
  writable by the configured container UID/GID.
- **Another instance is using the state directory**: stop the other process. Do not
  delete the lock file to bypass a live lock; the operating-system lock, not the
  file contents, establishes ownership.
- **A linked message fails**: confirm the user account can open that exact message
  and that it contains an actual Telegram file. A link's automatic web-page preview
  is not itself downloadable media; supported Telegram message links are routed to
  the linked target even when that preview is present.
- **A file is named `telegram_<id>.bin`**: Telegram supplied no usable filename and
  its extension or MIME type was unknown or unsafe. Rename the file afterward with
  `/rename` or the completion buttons.
- **Interrupted download does not restart automatically**: expected after restart
  recovery. Eligible interrupted jobs are paused pending until their requester
  continues them through the startup message or Job API. Exhausted jobs fail,
  persisted cancellations remain cancelled, and byte-range resume is not guaranteed.
- **A recovered confirmation or Working card is missing or not actionable**: the
  interaction row was restored but the best-effort edit or replacement send failed.
  Check for `outcome=not-visible` with a step, category, and error type; the log never
  contains IDs, names, or paths. Continue with `/files` or `/history` and then
  `/rename`, `/archive`, `/dirs`, or `/mkdir`. Do not edit or delete interaction rows
  manually, and do not claim visible recovery succeeded until the card appears.
- **A completed or retry result card is missing after restart**: the library result
  already committed and its `result-pending` or `retry-pending` row was retained
  because the final edit, replacement send, or sent-state cleanup was ambiguous. A
  replacement send that returned no positive message identity is logged only as a safe
  delivery failure category and leaves the row pending. The next start reconstructs the
  card from current catalog state without rerunning organization. Check only for
  `outcome=not-visible` or a stale-state cleanup warning with the step and error type,
  never IDs or paths. Do not delete the row manually; inspect `/files` or `/history`
  for the actual file state.
- **A page card shows stale controls after a failed update**: the adapter disables an
  unbound replacement, restores the source interaction, and re-renders its token. Use
  the visible Retry control. If no card is present, re-run `/files` or `/history`;
  do not delete interaction rows.
- **No startup initialization message**: confirm the user ID is in
  `telegram.authorized_users`, the Bot can initiate a private chat, and the log records
  only a generic startup-notification failure. Other users should still receive their
  own notices; do not restart solely to force one delivery.
- **Move all is missing after an album**: every member must complete successfully and
  the batch must contain more than one member. Partial, failed, or cancelled albums do
  not offer Move all. The durable notice outbox is redriven at startup and periodically
  while the service runs, so a transient Telegram failure does not require another
  completion or a process restart. A sent notice is terminal. If a send succeeded but
  its sent state was not recorded, a retry can deliver the notice again; the retry
  reuses the same interaction and callback identity, so a replayed button is idempotent
  and the state converges to sent. Treat this as at-least-once delivery across the
  Telegram/SQLite boundary, not exactly-once. The interaction is refreshed on reuse and
  expires after 24 hours without activity.
- **Move all reports skipped members**: skipped means already archived, missing from
  the catalog, or not ready. The operation is Inbox-only and per-file; investigate the
  reported counts rather than deleting database rows or files. A result with failed or
  not-ready members keeps the interaction and offers `Retry move`; selecting a
  directory whose list changed since it was rendered is rejected as stale without
  moving anything, so choose from the refreshed list.
