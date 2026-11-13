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
chmod 600 .env
chmod 644 config.yaml
```

`chmod 600 .env` restricts the Telegram credentials, Bot token, phone number, and
authorized users to the account that created the file. Compose loads the ignored
`.env` automatically.

`config.yaml` requires different handling. The portable Compose example bind-mounts
the host file `./config.yaml` read-only at `/config/config.yaml`, while the image runs
as the non-root UID/GID `10001`. A host-owned `config.yaml` with mode `600` is readable
only by the operator's own account, so the container user cannot read it and startup
fails. Keep `config.yaml` free of secrets, as the examples intend, and make it
readable with `chmod 644 config.yaml`; the owner keeps write access and the container
can read the mounted file.

A stricter alternative grants read access through ownership or group membership:
`sudo chown 10001:10001 config.yaml` together with `chmod 640 config.yaml`, or
`sudo chgrp 10001 config.yaml` with `chmod 640 config.yaml`. Running these commands
as shown requires `sudo` privilege to change ownership or group; the `chgrp` form
does not additionally require the operator to be a member of group `10001`. It
leaves the host owner as the file owner with write access, while the container
process reads through GID `10001`. The `chown` form moves the file to the
container's UID/GID, so a regular host account can then edit it only with `sudo`;
this is not a harmless adjustment when an unprivileged operator must keep editing
the file. Re-check mode and ownership whenever the service runs as a different user.

For a local Python process, keep credentials in restricted environment variables or
a restricted `.env` file loaded by your shell; do not store secrets in a
world-readable `config.yaml`. If `config.yaml` contains only non-secret settings,
mode `644` remains appropriate for the container-readability contract above. Compose
loads the ignored `.env` automatically. The local `compose.yaml`, `config.yaml`, and
`.env` files are intentionally ignored; only their sanitized examples belong in Git.

## Docker Hub deployment

The published multi-platform image is `shareur/tgharbor:0.2.1` in the
[Docker Hub repository](https://hub.docker.com/r/shareur/tgharbor). The portable
Compose example pulls that image; it does not build from the local checkout. It uses
project-scoped Docker-managed volumes named `tgharbor_data` and `tgharbor_downloads`,
contains no fixed host storage path, and runs as the image's non-root user (UID/GID
`10001`).

Pull the pinned release tag and optionally verify the resolved image identity before
starting:

```bash
docker compose pull
docker image inspect shareur/tgharbor:0.2.1 \
  --format '{{index .RepoDigests 0}}'
docker image inspect shareur/tgharbor:0.2.1 \
  --format '{{json .Config.Labels}}'
```

The published `0.2.1` release record identifies the multi-platform image index as
`sha256:b40996ef4938f8545e5eaee86c91fc57b3d27f9b087353d02a97ae7548c5f560`, with
aliases `0.2.1`, `0.2`, `latest`, and `sha-13c5bc7645b9` for `linux/amd64` and
`linux/arm64`. It records source revision
`13c5bc7645b9ba27ea9837be3697fa1388ffb44d` and source
`https://github.com/shareur/TGHarbor`. The `RepoDigests` entry should resolve to
that index, and the labels must report version `0.2.1`, that revision, and that
source. Tags are mutable aliases: pin `0.2.1` rather than `latest`, and write the
resolved digest into deployment notes so a later pull can be compared.

The first deployment needs an authorized user session. The image entrypoint is
`tgharbor` and its default command is `run`; `docker compose up -d` therefore starts
`tgharbor run`. Run the one-time login interactively before detached startup:

```bash
docker compose run --rm tgharbor login
docker compose up -d
docker compose logs -f tgharbor
```

The login command overrides the default command with `login` and persists the user
session under the configured sessions path. Run login only in a trusted interactive
terminal. The verification code and two-step password are passed directly to
Telethon and are not sent through the Bot.

Compose maps `config.yaml` read-only at `/config/config.yaml`. The SQLite database at
`/data/tgharbor.db` and both Telegram sessions under `/data/sessions` live in the
`tgharbor_data` volume; completed media and organization state live in the
`tgharbor_downloads` volume mounted at `/downloads`. Recreating the container
preserves both volumes, while `docker compose down -v` deletes them and must not be
used unless their contents are intentionally disposable or already backed up. Do not
scale the service above one replica: TGHarbor deliberately uses one
application-instance lock and multiple Workers inside that process.

A minimal post-rollout smoke check is
`docker run --rm --entrypoint tgharbor shareur/tgharbor:0.2.1 --help`, a
`docker compose ps` entry in `running` state, and a log line
`TGHarbor is running with N worker(s)` without repeated restarts or tracebacks.

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

The `install` command creates only the two data directories; the `./config.yaml` bind
source keeps the mode described above and must remain readable by the container user,
for example `chmod 644 config.yaml`. If an operator explicitly overrides the container
user, all bind sources, including `config.yaml`, must grant that same UID/GID read
access. Keep host-specific paths and identity overrides only in the ignored local
Compose file, never in `compose.example.yaml`.

## Source build alternative

The Docker Hub pull-and-run procedure above is the recommended operator path. Build
from source only for development or when intentionally producing a candidate image.
The Dockerfile's default labels identify an uncommitted local build; a release image
must instead be built from an immutable candidate with the version, revision, source,
and created arguments required by the
[release-security procedure](302-release-security.md#container-identity-and-provenance).

For example, check out the clean source revision for the intended release, build a
local image, and point the ignored `compose.yaml` at that local tag instead of the
Docker Hub image:

```bash
CANDIDATE_COMMIT="$(git rev-parse HEAD)"
test -z "$(git status --porcelain)"
docker build --pull \
  --build-arg TGHARBOR_IMAGE_VERSION=0.2.1 \
  --build-arg TGHARBOR_IMAGE_REVISION="$CANDIDATE_COMMIT" \
  --build-arg TGHARBOR_IMAGE_SOURCE=https://github.com/shareur/TGHarbor \
  --build-arg TGHARBOR_IMAGE_CREATED="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --tag tgharbor:0.2.1 .

# In the ignored local compose.yaml, use the local image tag:
# image: tgharbor:0.2.1
```

Then run the same one-time login, detached start, and log commands. A local tag is
not an immutable deployment identity; verify and record the resulting image digest
before any rollout. Local source builds are not release images.

## Local Python startup

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
- **Configuration cannot be read at startup**: the bind-mounted `config.yaml` must be
  readable by UID/GID `10001`. Replace a host-only `chmod 600` with
  `chmod 644 config.yaml`, or apply the ownership/group alternative, and confirm the
  file has no secrets before widening its mode.
- **Another instance is using the state directory**: stop the other process. Do not
  delete the lock file to bypass a live lock; the operating-system lock, not the
  file contents, establishes ownership.
- **A linked message fails**: confirm the user account can open that exact message
  and that it contains an actual Telegram file. A link's automatic web-page preview
  is not itself downloadable media; supported Telegram message links are routed to
  the linked target even when that preview is present.
- **Interrupted download restarts from zero**: expected. Job recovery is
  supported; byte-range resume is not guaranteed.
