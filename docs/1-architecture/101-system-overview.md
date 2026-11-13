# 101 — System overview

[简体中文](../zh-CN/1-architecture/101-system-overview.md)

TGHarbor separates Telegram-facing interfaces from download execution and shared-library
management.

```text
Telegram Bot and authorized user session
                 |
                 v
       Job API / Library API
          |            |
          v            v
  Persistent queue  SQLite catalog and audit
          |
          v
  Worker -> Provider -> Local storage
```

The diagram is conceptual. Interfaces submit and inspect work; providers retrieve
content; storage keeps files; SQLite retains queue and catalog state so accepted work
and completed files survive normal restarts.

## Component responsibilities

- **Telegram adapters** receive private Bot messages and resolve individual message
  links through the separate authorized user session.
- **Job API** creates and queries requester-scoped downloads.
- **Library API** exposes the shared completed-file catalog and organization actions.
- **Queue and workers** perform downloads outside interface handling.
- **Providers** hide source-specific retrieval behavior.
- **Storage** manages the configured local download root.

## Telegram roles and authorization

The Bot session receives media sent or forwarded by an authorized user and downloads
the Bot-visible message. The user session resolves one public or private `https://t.me/`
message link; the logged-in account must already have access to that message.

A Telegram user ID is checked before job creation, lookup, or cancellation. Jobs remain
requester-scoped. The Bot handles private conversations only; verification codes and
two-step-verification passwords are accepted only by the local `tgharbor login` command.

All configured authorized users share completed-file visibility and organization
rights. Mutating operations record the acting Telegram user ID and never infer job
ownership from shared-library access. This shared library is not a multi-tenant
isolation boundary.

## Durability and restart behavior

Accepted jobs are persistent. A normal restart does not lose queued work: eligible
interrupted jobs continue as new attempts, cancellation requests are honored, and jobs
that exhausted their attempts fail. Retryable failures use bounded delay, and a longer
Telegram flood wait is honored.

Interrupted downloads are not guaranteed to resume at a byte range: partial bytes may
be discarded and downloaded again. Only one application instance may use the same local
state. Terminal Bot notifications are best effort and cannot change a persisted result.

## Storage safety

Storage divides the configured root into shared `inbox/`, shared `archive/`, and hidden
`.partial/` areas. Completed downloads begin in Inbox, and authorized users organize
them into Archive.

Paths must remain below the configured root. Absolute paths, traversal, symbolic links,
and unexpected filesystem object types are rejected. A destination conflict receives a
suffix instead of overwriting an existing file. If completion or organization is
interrupted, TGHarbor recovers the safe result or leaves the affected file for operator
review without deleting user content. The database catalog and download root must be
backed up and restored as one consistency unit.
