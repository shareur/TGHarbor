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
cp compose.example.yaml compose.yaml
```

For a local Python process, put values in the ignored `config.yaml` or export the
documented environment variables. Compose loads the ignored `.env` automatically.
The local `compose.yaml`, `config.yaml`, and `.env` files are intentionally ignored;
only their sanitized examples belong in Git.

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

The example uses project-scoped Docker-managed volumes named `tgharbor_data` and
`tgharbor_downloads`. It contains no fixed host storage path and runs as the non-root
user built into the image. It pins the local image name to `tgharbor:0.2.0` rather
than a floating minor tag. The user session must exist before unattended startup:

```bash
chmod 600 .env config.yaml
docker compose build --pull
docker compose run --rm tgharbor login
docker compose up -d
docker compose logs -f tgharbor
```

The Compose build uses OCI provenance defaults that identify an uncommitted local
build. A release image must instead be built from an immutable candidate with the
revision, source, and created arguments required by the
[release-security procedure](302-release-security.md#container-identity-and-provenance).
The local `tgharbor:0.2.0` tag is not an immutable deployment identity; verify and
record the release image digest before any rollout.

Compose maps `config.yaml` read-only. The database and both Telegram sessions live in
the data volume; completed media and organization state live in the downloads volume.
Recreating the container preserves both volumes, while `docker compose down -v`
deletes them and must not be used unless their contents are intentionally disposable
or already backed up. Do not scale the service above one replica: TGHarbor deliberately
uses one application-instance lock and multiple Workers inside that process.

### Optional host bind mounts

Operators who require visible host paths can edit their ignored `compose.yaml` and
replace the two named-volume mounts with bind mounts:

```yaml
    volumes:
      - ./config.yaml:/config/config.yaml:ro
      - /srv/tgharbor/data:/data
      - /srv/tgharbor/downloads:/downloads
```

Create those directories for the image user before startup:

```bash
sudo install -d -o 10001 -g 10001 -m 0750 \
  /srv/tgharbor/data \
  /srv/tgharbor/downloads
```

If an operator explicitly overrides the container user, both bind sources must use
that same UID/GID. Keep host-specific paths and identity overrides only in the ignored
local Compose file, never in `compose.example.yaml`.

## Bot use

- Send or forward one media message in a private conversation with the Bot.
- Send one `https://t.me/<name>/<id>` or `https://t.me/c/<id>/<id>` link.
- Use `/status` for pending/running tasks, `/status <job-id>` for one task, and
  `/history [page]` for terminal history.
- Use `/cancel <job-id>` to cancel pending work or request cancellation of running
  work.
- Use `/files [page]`, `/dirs [page]`, and `/stats` to inspect the shared library.
- Use `/rename`, `/archive`, and `/mkdir`, or the completion-notification buttons, to
  organize files. Every authorized user shares this library; changes audit the actor.

The user account must already be able to open linked messages. The service does not
crawl history or bypass Telegram permissions.

## Backup and restore

Treat the complete data and downloads volumes as one restore point. Stop the service,
create both archives through one-off Compose containers, and securely copy the local
configuration files:

```bash
docker compose stop tgharbor
install -d -m 0700 backup
docker compose run --rm --no-deps --user 0:0 --entrypoint tar \
  -v "$PWD/backup:/backup" tgharbor \
  -cpf /backup/data-volume.tar -C /data .
docker compose run --rm --no-deps --user 0:0 --entrypoint tar \
  -v "$PWD/backup:/backup" tgharbor \
  -cpf /backup/downloads-volume.tar -C /downloads .
tar -cpf backup/private-config.tar .env config.yaml compose.yaml
sha256sum backup/*.tar > backup/SHA256SUMS
```

Verify `SHA256SUMS` before restoring. With the service stopped and the intended empty
or replaceable volumes mounted, use the same one-off pattern with `tar -xpf` into
`/data` and `/downloads`, restore the matching private configuration, and start the
service. Never combine the database from one backup with downloads from another.
Bind-mount deployments archive the complete host directories instead, preserving
ownership and permissions. On startup, running jobs, organization intents, and tracked
legacy outputs are reconciled before normal Bot handling. For schema-1 upgrades,
follow the [v0.2 upgrade and rollback guide](303-v0-2-upgrade.md).

## Troubleshooting

- **User session is not authorized**: rerun `tgharbor login` or the Compose login
  command in an interactive terminal.
- **Unauthorized**: add the sender's numeric ID to `authorized_users` or
  `TGHARBOR_TELEGRAM_AUTHORIZED_USERS`, then restart.
- **Permission denied under `/data` or `/downloads`**: for default named volumes,
  confirm the service uses the image's built-in user and recreate only a new empty
  volume after diagnosing ownership. For bind mounts, make both host directories
  writable by the configured container UID/GID.
- **Another instance is using the state directory**: stop the other process. Do not
  delete the lock file to bypass a live lock; the operating-system lock, not the
  file contents, establishes ownership.
- **A linked message fails**: confirm the user account can open that exact message
  and that it contains an actual Telegram file. A link's automatic web-page preview
  is not itself downloadable media; supported Telegram message links are routed to
  the linked target even when that preview is present.
- **Interrupted download restarts from zero**: expected. Job recovery is
  supported; byte-range resume is not guaranteed.
