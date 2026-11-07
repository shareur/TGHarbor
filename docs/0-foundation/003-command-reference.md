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

## Completion actions

A completed download shows readable metadata and persistent Keep, Rename, Archive,
and Rename + archive buttons. Rename and directory inputs are stored in SQLite until
confirmation, cancellation, or 24-hour expiry. Archive actions show existing directory
buttons plus New directory; sending a directory as text remains an accessible fallback.
Every callback rechecks authorization, expiry, actor ownership, and catalog state.

## Sanitized example

```text
User: /status
Bot:  [1a2b3c4d] Example video
      Status: running
      File: example.mp4
      Size: 128.0 MiB
      Progress: 42% · 53.8 MiB

Bot:  [9f8e7d6c] Conference recording · 512.0 MiB
      inbox/conference.mp4
      Choose how to organize this file: [Keep] [Rename] [Archive] [Rename + archive]
```

The example uses invented IDs, names, and sizes. Public screenshots or bug reports
must use similarly sanitized data.
