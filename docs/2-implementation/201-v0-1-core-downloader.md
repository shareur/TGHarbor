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

## Delivered outcomes

- Authorized private-Bot requests and single-message links can be downloaded without
  granting access to content the linked account cannot already see.
- Accepted work and terminal results survive normal process restarts. Interrupted jobs
  continue as new attempts, cancellation is honored, and retries remain bounded.
- Job status, progress, cancellation, and terminal notifications are visible in the Bot.
- Completed files stay below the configured local download root, and unsafe paths or
  overwrite attempts are rejected. Interrupted partial bytes may be discarded.
- Strict configuration rejects invalid values and keeps secrets out of rendered
  objects. Local login authorizes the user session once.
- The supplied container runs as a non-root user with persistent configuration, state,
  session, and download mounts.

## Automated acceptance

Run under Python 3.15 with development dependencies installed:

```bash
python -m ruff check .
python -m pytest -q
```

The suite uses fake Telegram clients and needs no credentials. It covers configuration
handling, requester authorization, persistence and restart behavior, bounded retries and
cancellation, provider routing, path and symbolic-link safety, Bot commands, and cleanup.

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
