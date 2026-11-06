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
mkdir -p data downloads
```

For a local Python process, put values in the ignored `config.yaml` or export the
documented environment variables. Compose loads the ignored `.env` automatically.
On Linux, set `TGHARBOR_UID` and `TGHARBOR_GID` to the owner of `data/` and
`downloads/`.

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

The supplied Compose file expects application files under any operator-selected
directory, while its bind-backed named volumes use `/data/tgharbor/data` for the
database and Telegram sessions and `/data/tgharbor/downloads` for completed media.
For the standard Ubuntu layout, place the application in `/opt/TGHarbor` and prepare
the persistent directories for the UID/GID configured in `.env`:

```bash
sudo install -d -o 1000 -g 1000 -m 0750 \
  /opt/TGHarbor \
  /data/tgharbor/data \
  /data/tgharbor/downloads
```

Replace `1000:1000` when `TGHARBOR_UID:TGHARBOR_GID` uses another identity. The
user session must exist before unattended service startup:

```bash
cd /opt/TGHarbor
chmod 600 .env config.yaml
docker compose build --pull
docker compose run --rm tgharbor login
sudo chmod 600 /data/tgharbor/data/sessions/*.session
docker compose up -d
docker compose logs -f tgharbor
```

Compose maps `config.yaml` read-only and mounts both `/data/tgharbor` bind sources
read-write through named volumes. Volume copy-up is disabled so first use cannot
replace host ownership with the image user's UID/GID. The database and both Telegram
sessions live under `/data/tgharbor/data`; recreating the container does not remove
them. Do not scale the service above one replica: v0.1 deliberately uses one
application-instance lock and multiple Workers inside that process.

## Bot use

- Send or forward one media message in a private conversation with the Bot.
- Send one `https://t.me/<name>/<id>` or `https://t.me/c/<id>/<id>` link.
- Use `/status` for recent tasks or `/status <job-id>` for one task.
- Use `/cancel <job-id>` to cancel pending work or request cancellation of running
  work.

The user account must already be able to open linked messages. The service does not
crawl history or bypass Telegram permissions.

## Backup and restore

Stop the service before making a filesystem-level backup:

```bash
docker compose stop tgharbor
```

Back up `config.yaml` securely, plus `data/` and `downloads/`. They contain secrets
and user content. Restore them with the same ownership and paths, then start the
service. On startup, running jobs are reconciled: retryable work returns to pending,
exhausted work fails, and persisted cancellation requests become cancelled.

## Troubleshooting

- **User session is not authorized**: rerun `tgharbor login` or the Compose login
  command in an interactive terminal.
- **Unauthorized**: add the sender's numeric ID to `authorized_users` or
  `TGHARBOR_TELEGRAM_AUTHORIZED_USERS`, then restart.
- **Permission denied under `/data` or `/downloads`**: make the host directories
  writable by `TGHARBOR_UID:TGHARBOR_GID`.
- **Another instance is using the state directory**: stop the other process. Do not
  delete the lock file to bypass a live lock; the operating-system lock, not the
  file contents, establishes ownership.
- **A linked message fails**: confirm the user account can open that exact message
  and that it contains an actual Telegram file. A link's automatic web-page preview
  is not itself downloadable media; supported Telegram message links are routed to
  the linked target even when that preview is present.
- **Interrupted download restarts from zero**: expected in v0.1. Job recovery is
  supported; byte-range resume is not guaranteed.
