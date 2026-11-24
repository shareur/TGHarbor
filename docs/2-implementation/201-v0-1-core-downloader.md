# 201 — v0.1 Core Downloader

[简体中文](../zh-CN/2-implementation/201-v0-1-core-downloader.md)

## Scope

v0.1 accepts work from authorized users in a private Bot conversation:

1. Send or forward one media message. The Bot session downloads that message.
2. Send one public or private Telegram message link. The user session downloads it
   only if the logged-in account can already access it.
3. Use `/status [job-id]` for persisted progress and `/cancel job-id` to request
   cancellation. Terminal results are also sent as best-effort Bot notifications.

Channel-history crawling, multi-message ranges, permission bypass, a web UI,
distributed Workers, and guaranteed byte-range resume are out of scope.

## Implemented contracts

- Strict YAML configuration with environment overrides and redacted secret fields.
- Local terminal user login with verification-code and optional two-step password
  callbacks; unattended startup only reuses an authorized session.
- Requester-scoped Job API and a versioned SQLite schema.
- Atomic concurrent claiming, bounded attempts, delayed retries, flood-wait support,
  cancellation, and restart recovery.
- Asynchronous Worker pool with timeout, progress throttling, cancellation polling,
  graceful shutdown, and Provider failure isolation.
- Per-job local storage with path and symbolic-link defenses, temporary files,
  atomic completion, and crash-window reconciliation.
- Non-root Python 3.15 container configuration with persistent configuration, state,
  session, and download mounts.

## Automated acceptance

Run under Python 3.15 with development dependencies installed:

```bash
python -m ruff check .
python -m pytest -q
```

The suite uses fake Telegram clients; it does not need or log credentials. It covers
configuration errors and secret rendering, independent SQLite claimers, retry
exhaustion, restart recovery, authorization and ownership, path traversal and
symbolic links, write failure, Worker timeout/cancellation/shutdown, source-session
routing, Bot commands, and application cleanup.

## Live acceptance

The following checks require operator-owned credentials and are not replaced by the
offline suite:

- Complete `tgharbor login`, including a two-step password when enabled.
- Start the Bot and reject a non-authorized Telegram user.
- Download media sent to the Bot and media referenced by a message link.
- Observe `/status`, cancellation, terminal notification, and a bounded retry.
- Stop the process during a download and confirm restart recovery.
- Build the image, run Compose as the configured host UID/GID, recreate the
  container, and confirm that sessions, SQLite state, and downloads persist.

Record any unperformed item as unverified rather than passing it by inference.
