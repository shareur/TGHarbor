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

## v0.1 adapters

v0.1 uses two independent Telegram sessions behind the same Provider contract:

- The Bot session receives media sent or forwarded by an authorized user and
  downloads the Bot-visible message.
- The user session resolves one public or private `https://t.me/` message link.
  The logged-in account must already have access to that message.

The authorization boundary is the Job API. A Telegram user ID is checked before
job creation, lookup, or cancellation. Jobs remain requester-scoped. The Bot only
handles private conversations; verification codes and two-step-verification
passwords are accepted only by the local `tgharbor login` command.

## Queue semantics

```text
pending --claim--> running --success--> completed
   |                  |  |
   |                  |  +--permanent/exhausted--> failed
   |                  +-----retryable-------------> pending (delayed)
   +--cancel--------------------------------------> cancelled
running --cancel request observed----------------> cancelled
```

SQLite stores the state, source reference, requester, attempt count, progress,
retry availability, ownership, error, and output path. One SQL statement claims a
pending row and assigns it to one Worker. Worker-owned state updates include both
the job ID and worker ID. v0.1 runs one application instance with multiple
asynchronous Workers; a database-adjacent process lock rejects a second instance.

At startup, interrupted running jobs below their attempt limit return to pending.
Exhausted jobs become failed, and persisted cancellation requests become cancelled.
This is job-level restart recovery: partial bytes are discarded and downloaded
again. Byte-range resume is not a v0.1 guarantee.

## Storage and completion

Each job writes to its own directory under the configured download root. Names are
sanitized, resolved paths must stay below the root, and symbolic-link targets are
rejected. A Worker writes a `.part` file, synchronizes it, atomically renames it,
and synchronizes the directory before marking the database row completed. If a
process stops after the rename but before the database update, the replacement
Worker recognizes the completed file and reconciles the row without downloading it
again.

Provider failures are classified as permanent or retryable. Retryable failures use
bounded exponential delay and honor a longer Telegram flood-wait interval. Terminal
Bot notifications are best effort and cannot change a persisted download result.
