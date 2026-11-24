# 101 — System overview

[简体中文](../zh-CN/1-architecture/101-system-overview.md)

TGHarbor separates external interfaces from the download domain.

```text
Telegram / future REST API / CLI
              |
              v
           Job API
              |
              v
        Persistent Queue
              |
              v
           Worker
              |
              v
          Provider
              |
              v
           Storage
```

## Principles

1. Telegram is an adapter, not the application core.
2. Download jobs are persistent domain objects.
3. Workers can recover unfinished jobs after restart.
4. Providers hide source-specific download behavior.
5. Storage routing is independent from providers.
6. UI/API layers should not own download state.

The shared-library path is separate from download-state ownership:

```text
Telegram Bot
     |
     v
 Library API ------> SQLite catalog and audit
     |                         |
     +-------------------------+
                   |
                   v
             Local Storage
```

## Telegram adapters

TGHarbor uses two independent Telegram sessions behind the same Provider contract:

- The Bot session receives media or albums sent or forwarded by an authorized user.
  Albums are handled once by a dedicated album handler; grouped messages are ignored
  by the single-message handler. It downloads the Bot-visible messages.
- The user session resolves one public or private `https://t.me/` message link.
  The logged-in account must already have access to that message.

The authorization boundary is the Job API. A Telegram user ID is checked before
job creation, lookup, or cancellation. Jobs remain requester-scoped. The Bot only
handles private conversations; verification codes and two-step-verification
passwords are accepted only by the local `tgharbor login` command.

Album submission derives one deterministic opaque batch ID from a fixed version
prefix, requester ID, configured session/client identity, source chat, and Telegram
`grouped_id`. Those components are never placed in callback payloads or logs. Every
media member uses that ID and the exact album size. A direct link resolving to one
member remains a single one-member job; no remote album crawl is performed.

The Library API forms a second authorization boundary. All configured authorized
users share completed-file visibility and organization rights. Mutating operations
record the acting Telegram user ID, validate catalog state again, and never infer job
ownership from shared-library access.

## Queue semantics

```text
pending --claim--> running --success--> completed
   |                  |  |
   |                  |  +--permanent/exhausted--> failed
   |                  +-----retryable-------------> pending (delayed)
   +--cancel--------------------------------------> cancelled
running --cancel request observed----------------> cancelled
```

SQLite stores the state, source reference, requester, bounded display metadata,
attempt count, progress, retry availability, ownership, error, and output path. One
SQL statement claims a pending row and assigns it to one Worker. Worker-owned state
updates include both the job ID and worker ID. TGHarbor runs one application instance with multiple
asynchronous Workers; a database-adjacent process lock rejects a second instance.

At startup, interrupted running jobs below their attempt limit return to pending
with `resume_required` set. They are excluded from normal claims until their
requester lists and explicitly continues all or selected paused jobs. Continuing
clears the pause flag, preserves attempt accounting, and wakes Workers; repeated
continuation is idempotent. Jobs that exhausted attempts become failed, and
persisted cancellation requests become cancelled. Ordinary pending jobs are
unaffected. This is job-level restart recovery: partial bytes are discarded and
the download starts again after continuation. Byte-range resume is not a current
guarantee.

After recovery and once Telegram clients can send, startup sends each configured
authorized user one best-effort private initialization message with the package
version, a server-local ISO timestamp with timezone, and that requester's paused
count. A positive count includes persistent View and Continue interactions; zero
states that no interrupted tasks exist and omits them. One failed chat is logged
generically and does not abort startup or affect other users. Workers start before
these notices, so ordinary pending jobs are not blocked and no second recovery pass
runs.

Each attempt records `started_at` when claimed and sets `finished_at` atomically
on completion, terminal failure, or cancellation. A retry clears the current
attempt start so elapsed-time and speed reporting describe the attempt that
actually ran. The completion timestamp persisted with the completion transaction
is the authoritative success time.

A batch is a set of explicitly submitted jobs sharing one opaque requester-scoped
identity. Core never derives identity from paths. Group and channel identifiers are
stored as text metadata only: `origin_group_id` is the original group or channel
peer when Telegram supplies it, while `channel_id` is an original/source channel
only. Core never infers one from the other, from the requester, or from a private
chat; unavailable or ambiguous values render `N/A`. A batch is eligible for a
completion action only when its expected size is greater than one, exactly that many
jobs exist, all are terminal, and every member completed. A compact per-requester
notice row is the durable outbox for that action. One transaction checks eligibility
and moves the notice through `pending`, `claimed`, and `sent`; a failed send or a
failed sent-state mark releases the claim.

A lifecycle-managed dispatcher discovers eligible pending notices and stale claims
in bounded pages, redrives them at startup and on a fixed poll while the service is
running, and is woken by job completion. Delivery therefore does not depend on a
later completion callback. Sent notices are terminal. The dispatcher creates or
reuses exactly one opaque Move-all interaction across attempts and never treats a
persisted interaction as proof that Telegram accepted a message. Delivery is
at-least-once: a crash after Telegram accepted a send but before SQLite recorded
`sent` can duplicate the chat notice, but the retry keeps the same interaction and
callback identity and converges to `sent`. Exactly-once delivery across the external
send and the local commit is not claimed.

## Storage and completion

Storage divides the configured root into shared `inbox/`, shared `archive/`, and
hidden `.partial/` areas. A Worker reserves a collision-safe Inbox name in SQLite,
writes into a per-job partial area, synchronizes the file, and commits it with
no-overwrite semantics before completing the job and catalog row in one database
transaction. Resolved paths must stay below the root; symbolic links and unexpected
filesystem object types are rejected.

Rename and archive operations persist a filesystem intent before moving a file, then
update the catalog and actor audit before removing that intent. Startup reconciliation
finishes a safe interrupted move or rejects changed and ambiguous state. A separate
one-time reconciler moves tracked v0.1 UUID-directory outputs into Inbox without
overwriting unrelated files.

Provider failures are classified as permanent or retryable. Retryable failures use
bounded exponential delay and honor a longer Telegram flood-wait interval. Terminal
Bot notifications are best effort and cannot change a persisted download result. The
success message uses the final catalog name, a logical `/download/<relative parent>`
folder, stored size, persisted attempt start/finish times converted to server-local
`HH:MM:SS`, elapsed seconds, and average KB/s. Missing values render `N/A`; host
absolute paths and notification wall-clock times are never used.
