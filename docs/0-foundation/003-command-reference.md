# 003 — Bot command reference

[简体中文](../zh-CN/0-foundation/003-command-reference.md)

All commands are accepted only in private Bot conversations from configured numeric
Telegram user IDs. A job is visible/cancellable only to its requester. The file
library is shared by all authorized users, and mutating operations record the actor ID.

## Job commands

| Command | Behavior |
| --- | --- |
| `/start`, `/help` | Show input and command help. |
| `/status` | List up to 10 requester-owned pending/running jobs. |
| `/status <id>` | Resolve one requester-owned full or unambiguous short job ID. |
| `/history [page]` | List requester-owned terminal jobs, 10 per page, with persistent navigation buttons. |
| `/cancel <id>` | Cancel a pending job or request cancellation of a running requester-owned job. |
| `/cancel` | Cancel the active rename/directory prompt. Without an active prompt it shows the job-cancel usage. |

The Bot also registers a best-effort command menu for the default and all-private-chat
scopes using the exact commands `start`, `files`, `status`, `history`, `dirs`,
`cancel`, `stats`, `rename`, `archive`, `mkdir`, and `help`. Registration failures log
only outcome, scope, and error type; they never block startup or typed commands.

## Shared library commands

| Command | Behavior |
| --- | --- |
| `/files [page]` | List shared catalog files, 10 per page, with persistent navigation buttons. |
| `/dirs [page]` | List archive directories, 10 per page. |
| `/stats` | Show file count, total bytes, Inbox count, and Archive count. |
| `/rename <file-id> <new-name>` | Rename a shared file and audit the actor. |
| `/archive <file-id> <directory>` | Move a shared file into an existing archive directory and audit the actor. |
| `/mkdir <directory>` | Safely create a relative archive directory and audit the actor. |

IDs may be shortened only when the prefix uniquely resolves within the command's
authorization scope. Absolute paths, dot/traversal components, symbolic links, and
unsupported names are rejected. Commands do not silently overwrite existing files.

## Album intake

Sending a Telegram album queues every media message once with one opaque batch identity
and the exact album member count. A single message or a supported message link remains a
one-member job; a linked album member is not expanded by crawling the remote album.
Accepted link/upload messages reply `Download queued · <short-id>` with the source,
pending status, and next `/status <short-id>` guidance. An album replies
`Album queued · <count> files` and states that Move all appears after every file
completes.

## Completion message and actions

A successful download always shows these fields in this order and with these English
labels:

```text
Download Completed
Job: <short job id>
File Name: <final catalog name>
Download Folder: /download/<relative parent>
File Size: <MiB> MB
Start Time: <server-local HH:MM:SS>
End Time: <server-local HH:MM:SS>
Download Time: <seconds>
Download Speed: <KiB/s> KB/s
User Id: <requester>
Origin Group: <value or N/A>
Channel ID: <value or N/A>
```

The folder is always a logical `/download/` path, never a host absolute path. Times use
the server's local timezone, and elapsed time/speed are derived from persisted attempt
timestamps and the stored catalog size. Missing values render `N/A`.

The message also offers persistent Keep, Rename, Archive, and Rename + archive buttons.
These actions, and a batch `Move all to new folder` action, edit the originating
completion card in place; only an uneditable message falls back to one replacement
send. A concise `Job <short-id> · <file>` or `Album · <count> files` header stays
visible. The directory chooser states the next action, lists existing directories in
recent-use order, includes `New directory` and `Cancel`, and says
`No archive directories exist yet.` when none exist. The new-directory prompt reads
`Send a relative path, for example: videos/2026. Do not start with /`.
Invalid or slash-prefixed input stays on that prompt with the same example and never
creates a job. Confirmation for an existing directory uses `Move here`; a new directory
uses `Create and move` and notes that it will be created. Both offer `Back` and
`Cancel`. Before library work the card shows `Working…`; the result then replaces it.
Directory buttons carry an opaque digest bound to the displayed directory and ordering,
so a reordered or changed list rejects the old selection as stale and shows refreshed
choices instead of choosing another directory. Confirmation moves only current Inbox
members through the Library API, reports moved, skipped, and failed counts without host
paths, and rejects stale, cross-action, or cross-requester callbacks without mutation.
A result with failed or not-ready members keeps a persistent `Retry move` action on the
same card; only a fully moved or already-moved result consumes the interaction. Rename
and directory inputs are stored in SQLite until confirmation, cancellation, or 24-hour
expiry. Every callback rechecks authorization, expiry, actor ownership, and catalog
state. Keep preserves the completion summary and states that the file remains in Inbox.

## Startup controls

After restart recovery plus Telegram startup, each authorized user receives one private
initialization message with the package version, a server-local ISO timestamp with
timezone, and that requester's interrupted count. When the count is positive, persistent
`View interrupted tasks` and `Continue interrupted tasks` buttons are included; a zero
count states that no interrupted tasks exist. View is requester-scoped and shows no host
paths or other users' jobs. Continue resumes only that requester's paused jobs and wakes
Workers. Repeated views or continues are idempotent, and continuing again reports zero.

## Sanitized example

```text
User: /status
Bot:  [1a2b3c4d] Example video
      Status: running
      File: example.mp4
      Size: 128.0 MiB
      Progress: 42% · 53.8 MiB

Bot:  Download Completed
      Job: 9f8e7d6c
      File Name: conference.mp4
      Download Folder: /download/inbox
      File Size: 512.00 MB
      Start Time: 14:02:11
      End Time: 14:03:27
      Download Time: 76.00
      Download Speed: 6898.53 KB/s
      User Id: 1001
      Origin Group: N/A
      Channel ID: N/A
      [Keep] [Rename] [Archive] [Rename + archive]
```

The example uses invented IDs, names, and sizes. Public screenshots or bug reports
must use similarly sanitized data.
