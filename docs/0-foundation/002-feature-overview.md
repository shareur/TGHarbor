# 002 — Feature overview

[简体中文](../zh-CN/0-foundation/002-feature-overview.md)

This document describes the behavior implemented in TGHarbor v0.1 Core Downloader.
Code and reproducible checks remain authoritative; features listed under
[Current limitations](#current-limitations) are intentionally not implemented.

## Product model

TGHarbor is a self-hosted Telegram download service operated through a private Bot
conversation. Authorized users submit media, receive a persistent job ID, inspect
progress, cancel work, and receive a final Bot notification. A local SQLite queue,
asynchronous Workers, and safe filesystem storage keep work recoverable across
normal process restarts.

v0.1 uses two independent Telegram clients:

| Client | Authentication | Responsibility |
| --- | --- | --- |
| Bot client | BotFather token | Bot conversation, commands, direct/forwarded media, and notifications |
| User client | One-time phone, verification code, and optional two-step password | Media referenced by individual Telegram message links |

The phone number does not authenticate the Bot. It authorizes the separate user
session once through a trusted local terminal. The saved session is reused during
unattended service startup.

## Access and authorization

- The Bot handles private conversations only. Messages from groups or channels are
  ignored.
- Every submit, status, and cancellation operation checks the sender's numeric
  Telegram user ID against `telegram.authorized_users`.
- Jobs are requester-scoped. An authorized user cannot inspect or cancel another
  user's job by supplying its ID.
- An unauthorized private sender receives `Unauthorized.` and no job is created.
- Telegram verification codes and two-step-verification passwords are accepted only
  by the local `tgharbor login` command, never through the Bot.

## Download sources

### Media sent to the Bot

An authorized user can send or forward one Telegram message containing media in a
private Bot conversation. TGHarbor records the Bot-visible chat and message ID and
downloads the media through the Bot client.

Only messages exposing an actual Telegram file are accepted as Bot-media jobs.
Web-page previews without a file are not downloadable media. Text that is not a
supported command is evaluated as a single-message link first and receives an
explanatory error plus help text when invalid.

### Individual message links

An authorized user can submit exactly one of these HTTPS link forms:

- Public chat: `https://t.me/<username>/<message-id>`
- Private chat: `https://t.me/c/<internal-chat-id>/<message-id>`

`www.t.me` and `telegram.me` hosts are also recognized. Links must identify one
positive message ID and cannot contain ranges, whitespace, credentials, a custom
port, query parameters, or a fragment. The user account must already be able to
open the message, and the message must contain downloadable media. TGHarbor does not
bypass Telegram permissions. Telegram may attach a web-page preview to a submitted
link; TGHarbor still routes the link itself through the user account rather than
mistaking that preview for Bot-visible media.

### Duplicate submissions

The queue deduplicates a source for each requester using the requester ID, source
client, source chat, and source message ID. Repeating the same submission returns
the existing persistent job instead of creating another download.

## Bot commands and responses

| Input | Behavior |
| --- | --- |
| `/start` or `/help` | Show accepted inputs and command help. |
| `/status` | Show up to the 10 most recent jobs belonging to the requester. |
| `/status <job-id>` | Show one requester-owned job, or report an invalid/unknown ID. |
| `/cancel <job-id>` | Cancel a pending job immediately or request cancellation of a running job. |
| Media message | Create or return the matching Bot-session download job. |
| Message link | Create or return the matching user-session download job. |

Submission replies include the persistent UUID job ID and current status. Status
output includes bytes downloaded and, when Telegram supplies a total size, an
integer percentage. Failed jobs include a sanitized error. On completion, failure,
or cancellation, TGHarbor sends a best-effort private notification; notification
failure cannot change the stored job result.

## Persistent job lifecycle

| Status | Meaning and transitions |
| --- | --- |
| `pending` | Waiting for an eligible Worker and retry availability time. Can be claimed or cancelled. |
| `running` | Atomically owned by one Worker. Can complete, fail, return to pending for retry, or observe cancellation. |
| `completed` | Final file was committed and its output path was persisted. Terminal. |
| `failed` | A permanent error occurred or the configured attempt limit was exhausted. Terminal. |
| `cancelled` | A pending cancellation was immediate, or a running Worker stopped and discarded its partial file. Terminal. |

SQLite stores source identity, requester, status, attempt counts, progress, retry
time, Worker ownership, cancellation request, error, output path, and timestamps.
The schema is versioned and refuses to open a database created by a newer unsupported
schema version.

One SQL statement claims the oldest eligible pending job and assigns it to exactly
one Worker. State-changing Worker operations also require the same Worker ID, which
prevents stale or competing Workers from completing another Worker's job.

## Workers, progress, retry, and cancellation

- A configurable asynchronous Worker pool processes multiple jobs concurrently in
  one application instance.
- New submissions wake idle Workers; idle polling provides a fallback.
- Each download attempt has a configurable timeout.
- Download progress is throttled before being persisted, then flushed after the
  provider finishes.
- Running cancellation is polled while the Telethon download task is active. The
  task is cancelled, its `.part` file is removed, and the job becomes `cancelled`.
- Retryable failures use bounded exponential delay:
  `retry_base_seconds * 2^(attempt_count - 1)`.
- Telegram flood waits are honored when longer than the calculated retry delay.
- Telegram rate limits, server errors, timeouts, operating-system I/O errors, and
  unexpected adapter failures are retryable. Invalid/inaccessible sources and
  messages without media are permanent failures.
- Retries stop at `downloads.max_attempts`; permanent failures do not consume
  unnecessary later attempts.
- Graceful Worker shutdown discards the current partial file and returns eligible
  work to the persistent retry path.

Errors returned to users and logs are normalized and bounded; raw third-party
exception details and credentials are not intentionally exposed.

## Restart and crash recovery

At startup TGHarbor reconciles jobs left in `running`:

- A persisted cancellation request becomes `cancelled`.
- A job below its attempt limit returns to `pending` immediately.
- A job at its attempt limit becomes `failed`.

Recovery is job-level rather than byte-level: interrupted `.part` data is discarded
and the next attempt normally starts from zero. If the process stopped after the
final atomic rename but before the database completion update, the replacement
Worker recognizes the completed regular file and marks the job completed without
downloading it again.

## Local storage

- Every job writes below the configured download root in a directory named with the
  job UUID.
- Telegram filenames are Unicode-normalized, path separators and control characters
  are replaced, unsafe empty/dot names fall back to `download.bin`, and UTF-8 names
  are bounded to 180 bytes while preserving a reasonable suffix.
- Resolved paths must remain under the download root and inside exactly one job
  directory.
- Download roots, job directories, temporary files, and completed files must have
  the expected real filesystem type. Symbolic-link targets are rejected.
- Downloads first write to a hidden `.part` file. TGHarbor synchronizes the file,
  atomically replaces the final path, and synchronizes the containing directory
  before persisting completion.
- Failed, cancelled, timed-out, and interrupted attempts remove their temporary file
  when safe to do so. Completed files are not overwritten on reconciliation.

## Configuration

TGHarbor loads strict YAML, applies supported environment overrides, rejects unknown
keys, and stops on missing or invalid required values. Relative paths resolve from
the YAML file's directory. Secret dataclass fields are excluded from object
representations.

### Telegram settings

| YAML setting | Environment override | Required | Purpose |
| --- | --- | --- | --- |
| `telegram.api_id` | `TGHARBOR_TELEGRAM_API_ID` | Yes | Numeric Telegram application ID used by both clients. |
| `telegram.api_hash` | `TGHARBOR_TELEGRAM_API_HASH` | Yes | Telegram application secret used by both clients. |
| `telegram.bot_token` | `TGHARBOR_TELEGRAM_BOT_TOKEN` | Yes | BotFather token for the Bot client. |
| `telegram.phone` | `TGHARBOR_TELEGRAM_PHONE` | No | User-account login input; the login command prompts when omitted. |
| `telegram.authorized_users` | `TGHARBOR_TELEGRAM_AUTHORIZED_USERS` | Yes | Non-empty list, or comma-separated environment value, of positive numeric user IDs. |

### Runtime settings

| YAML setting | Default | Environment override |
| --- | --- | --- |
| `downloads.path` | `./downloads` | `TGHARBOR_DOWNLOADS_PATH` |
| `downloads.concurrency` | `2` | `TGHARBOR_DOWNLOADS_CONCURRENCY` |
| `downloads.timeout_seconds` | `3600` | `TGHARBOR_DOWNLOADS_TIMEOUT_SECONDS` |
| `downloads.max_attempts` | `3` | `TGHARBOR_DOWNLOADS_MAX_ATTEMPTS` |
| `downloads.retry_base_seconds` | `5` | `TGHARBOR_DOWNLOADS_RETRY_BASE_SECONDS` |
| `database.path` | `./data/tgharbor.db` | `TGHARBOR_DATABASE_PATH` |
| `sessions.path` | `./data/sessions` | `TGHARBOR_SESSIONS_PATH` |
| `logging.level` | `INFO` | `TGHARBOR_LOG_LEVEL` |

Positive numeric settings reject zero and negative values. Log level accepts only
`CRITICAL`, `ERROR`, `WARNING`, `INFO`, or `DEBUG`. `TGHARBOR_CONFIG` selects a YAML
file other than `./config.yaml`.

## Command-line interface

| Command | Behavior |
| --- | --- |
| `tgharbor login [--config PATH]` | Interactively authorize and persist the user-account session. |
| `tgharbor run [--config PATH]` | Start queue recovery, both Telegram clients, Workers, and Bot handling. |
| `tgharbor [--config PATH]` | Start the service; `run` is the default command. |

Unattended startup never prompts for Telegram verification. It requires an already
authorized user session and stops with an actionable error when the session is
missing.

## Runtime and deployment

- Python 3.15 or newer is required; CI checks the minimum Python 3.15 runtime.
- The application holds a non-blocking database-adjacent file lock and rejects a
  second TGHarbor process using the same state directory.
- The image runs as a non-root user and uses `tini`-style Compose initialization.
- Compose supports host UID/GID selection, optional `.env` loading, read-only YAML
  configuration, persistent state/session storage, persistent downloads,
  `no-new-privileges`, a restart policy, and a graceful stop period.
- Compose overrides runtime paths to `/data/tgharbor.db`, `/data/sessions`, and
  `/downloads`. The supplied named volumes use host bind sources configured in
  `compose.yaml`.
- Configuration, the SQLite database and sidecars, Telegram sessions, and downloaded
  user content are excluded from Git. Sanitized examples remain tracked.

For installation, login, Docker commands, host directory ownership, backup, restore,
and troubleshooting, follow the [Core Downloader operations guide](../3-operations/301-core-downloader-operations.md).

## Security and data boundaries

- Authorization is checked before a request reaches queue operations.
- Telegram access is limited to what the Bot or logged-in user account can already
  read; no permission bypass is attempted.
- Job lookup and cancellation include requester ownership checks.
- Credentials, session files, runtime databases, logs, and downloaded content are
  Git-ignored. Operators must still protect `.env`, `config.yaml`, session storage,
  backups, and host permissions.
- Configuration and third-party errors avoid rendering secret values.
- Storage path, filename, file-type, and symbolic-link checks protect the configured
  download root.
- Best-effort notifications are isolated from durable job state.

## Persistent data and backup scope

The following data survives normal container recreation when mounted correctly:

- SQLite database: jobs, status, progress, retries, ownership, and output paths.
- Telegram session directory: Bot and authorized user sessions.
- Download directory: completed media organized by job ID.

Stop the service before a filesystem-level backup, preserve ownership and paths, and
treat all three locations plus local configuration as sensitive. Detailed procedures
are in the [operations guide](../3-operations/301-core-downloader-operations.md#backup-and-restore).

## Current limitations

v0.1 intentionally does not provide:

- Channel or chat history crawling.
- Message ranges, albums as an explicit batch, or bulk import.
- Telegram permission bypass.
- A Bot-only runtime mode; the selected single-message-link feature currently makes
  an authorized user session mandatory at startup.
- Guaranteed byte-range resume of interrupted media.
- Multiple application replicas or distributed Workers. Use one process with its
  internal asynchronous Worker pool.
- Cloud/object storage backends; only local filesystem storage is implemented.
- A web UI, REST API, public CLI job-management interface, or scheduler.
- Automatic archive indexing, search, tagging, deduplication by file content, or
  retention policies.
- Live Telegram or Docker-host acceptance without operator-owned credentials and a
  Docker daemon. Automated tests use fake Telegram clients.

## Related documents

- [Development setup](001-development-setup.md)
- [System overview](../1-architecture/101-system-overview.md)
- [v0.1 implementation and acceptance](../2-implementation/201-v0-1-core-downloader.md)
- [Core Downloader operations](../3-operations/301-core-downloader-operations.md)
