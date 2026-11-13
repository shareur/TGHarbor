# Security policy

## Supported versions

TGHarbor is pre-1.0. Security fixes target the current default branch. Older snapshots
may require upgrading rather than receiving a backport.

## Reporting a vulnerability

Do not open a public issue containing exploit details, credentials, Telegram sessions,
private links, user content, database rows, logs, or deployment identifiers. Until a
dedicated security-reporting address is published, send only a minimal, non-sensitive
notice through the hosting platform's confidential reporting channel for this project.
The maintainer will provide a confidential path for details.

Include affected version/commit, impact, prerequisites, and a sanitized reproduction.
Do not test against accounts, Bots, hosts, or content you do not own or administer.

## Operator responsibilities

Protect API hashes, Bot tokens, phone numbers, session files, configuration, databases,
backups, logs, and downloads. If exposure is possible, revoke or rotate credentials;
history rewriting alone does not make an active credential safe. Follow the release
security checklist before publishing a repository, archive, or image.
