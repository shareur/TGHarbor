# 301 — 核心下载器运维

[English](../../3-operations/301-core-downloader-operations.md)

## 前置条件

创建 Telegram 应用以取得 API ID 和 API Hash，并通过 BotFather 创建 Bot Token。确定每位允许用户的数字 Telegram ID。不得提交这些值、`.env`、`config.yaml`、会话文件、数据库或下载内容。

复制并编辑示例：

```bash
cp config.example.yaml config.yaml
cp .env.example .env
cp compose.example.yaml compose.yaml
chmod 600 .env
chmod 644 config.yaml
```

`chmod 600 .env` 让 Telegram 凭据、Bot Token、手机号和授权用户只对创建该文件的账号可读。Compose 会自动加载已忽略的 `.env`。

`config.yaml` 需要另一种处理。可移植 Compose 示例把宿主文件 `./config.yaml` 以只读方式 bind mount 到 `/config/config.yaml`，而镜像以非 root 的 UID/GID `10001` 运行。属于宿主运维账号且权限为 `600` 的 `config.yaml` 只对该账号可读，容器用户无法读取，启动会失败。请按示例的设计让 `config.yaml` 不含密钥，并用 `chmod 644 config.yaml` 使其可读：所有者仍可写入，容器也能读取挂载文件。

更严格的替代方案通过所有权或组归属授予读取权限：`sudo chown 10001:10001 config.yaml` 配合 `chmod 640 config.yaml`，或 `sudo chgrp 10001 config.yaml` 配合 `chmod 640 config.yaml`。按上述命令执行时需要 `sudo` 特权来更改所有权或所属组；`chgrp` 方式不额外要求运维账号属于 `10001` 组。`chgrp` 方式保留宿主所有者身份及其写权限，容器进程则通过 GID `10001` 读取。`chown` 方式会把文件划归容器 UID/GID，此后普通宿主账号只能通过 `sudo` 编辑；当非特权运维者仍需继续编辑该文件时，这不是无损的调整。服务以其他用户运行时须重新核对权限与所有权。

本地 Python 进程应把凭据保存在权限受限的环境变量或由 shell 加载的受限 `.env`（例如权限 `600`）中，不要把密钥写入全局可读的 `config.yaml`。若 `config.yaml` 只包含非敏感设置，`644` 仍符合上述容器可读性约定。Compose 会自动加载已忽略的 `.env`。本地 `compose.yaml`、`config.yaml` 和 `.env` 均应保持忽略状态；Git 中只保留脱敏示例。

## 通过 Docker Hub 部署

已发布的多平台镜像位于 [Docker Hub 仓库](https://hub.docker.com/r/shareur/tgharbor) 的 `shareur/tgharbor:0.2.1`。可移植 Compose 示例直接拉取该镜像，不在本地构建。示例使用项目范围内由 Docker 管理的 `tgharbor_data` 与 `tgharbor_downloads` 命名卷，不包含固定宿主存储路径，并以内建非 root 镜像用户（UID/GID `10001`）运行。

拉取固定的发布标签，并在启动前可选核对镜像身份：

```bash
docker compose pull
docker image inspect shareur/tgharbor:0.2.1 \
  --format '{{index .RepoDigests 0}}'
docker image inspect shareur/tgharbor:0.2.1 \
  --format '{{json .Config.Labels}}'
```

已发布的 `0.2.1` 发布记录标明多平台镜像索引为 `sha256:b40996ef4938f8545e5eaee86c91fc57b3d27f9b087353d02a97ae7548c5f560`，别名 `0.2.1`、`0.2`、`latest` 和 `sha-13c5bc7645b9` 均指向该索引，平台为 `linux/amd64` 与 `linux/arm64`，源码 revision 为 `13c5bc7645b9ba27ea9837be3697fa1388ffb44d`，来源为 `https://github.com/shareur/TGHarbor`。`RepoDigests` 条目应解析到该索引，标签必须报告 version `0.2.1`、上述 revision 和来源。标签只是可变别名：应固定 `0.2.1` 而不要使用 `latest`，并把解析后的摘要写入部署记录，以便后续拉取时比对。

首次部署需要已授权的用户会话。镜像入口点为 `tgharbor`，默认命令为 `run`；因此 `docker compose up -d` 会运行 `tgharbor run`。在后台启动前，先在可信的交互式终端执行一次性登录：

```bash
docker compose run --rm tgharbor login
docker compose up -d
docker compose logs -f tgharbor
```

登录命令会用 `login` 覆盖默认命令，并把用户会话持久化到配置的 sessions 路径。只能在可信的交互式终端执行登录。验证码和两步验证密码会直接交给 Telethon，不会通过 Bot 发送。

Compose 以只读方式把 `config.yaml` 映射到 `/config/config.yaml`。SQLite 数据库 `/data/tgharbor.db` 与两个 Telegram 会话 `/data/sessions` 位于 `tgharbor_data` 卷；完成媒体与整理状态位于挂载到 `/downloads` 的 `tgharbor_downloads` 卷。重建容器会保留两个卷；`docker compose down -v` 会删除它们，除非数据明确可丢弃或已有备份，否则不得使用。不要把服务扩展到多个副本：TGHarbor 明确使用一个应用实例锁，并在该进程内运行多个 Worker。

最小的上线后冒烟检查是 `docker run --rm --entrypoint tgharbor shareur/tgharbor:0.2.1 --help`、`docker compose ps` 中状态为 `running`，以及日志出现 `TGHarbor is running with N worker(s)`，且没有反复重启或 traceback。

### 可选宿主 bind mount

需要直接查看宿主路径的运维者，可编辑被忽略的本地 `compose.yaml`，把两个命名卷替换为 bind mount：

```yaml
    volumes:
      - ./config.yaml:/config/config.yaml:ro
      - /srv/tgharbor/data:/data
      - /srv/tgharbor/downloads:/downloads
```

启动前为镜像用户创建目录：

```bash
sudo install -d -o 10001 -g 10001 -m 0750 \
  /srv/tgharbor/data \
  /srv/tgharbor/downloads
```

该 `install` 命令只创建两个数据目录；`./config.yaml` bind 来源保持上文所述的权限，并且必须对容器用户可读，例如 `chmod 644 config.yaml`。若显式覆盖容器用户，包括 `config.yaml` 在内的所有 bind 来源都必须向该同一 UID/GID 授予读取权限。宿主专用路径及身份覆盖只能写入被忽略的本地 Compose 文件，不得进入 `compose.example.yaml`。

## 本地源码构建

上面的 Docker Hub 拉取与运行步骤是推荐的运维路径。只在开发或有意生成候选镜像时从源码构建。Dockerfile 的默认标签表示未提交的本地构建；发布镜像必须从不可变候选构建，并按[发布安全流程](302-release-security.md#容器身份与来源)提供 version、revision、source 和 created 参数。

例如，检出目标发布的干净源码 revision，构建本地镜像，并把被忽略的 `compose.yaml` 指向该本地标签而不是 Docker Hub 镜像：

```bash
CANDIDATE_COMMIT="$(git rev-parse HEAD)"
test -z "$(git status --porcelain)"
docker build --pull \
  --build-arg TGHARBOR_IMAGE_VERSION=0.2.1 \
  --build-arg TGHARBOR_IMAGE_REVISION="$CANDIDATE_COMMIT" \
  --build-arg TGHARBOR_IMAGE_SOURCE=https://github.com/shareur/TGHarbor \
  --build-arg TGHARBOR_IMAGE_CREATED="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --tag tgharbor:0.2.1 .

# 在被忽略的本地 compose.yaml 中使用本地镜像标签：
# image: tgharbor:0.2.1
```

然后执行相同的一次性登录、后台启动和日志命令。本地标签不是不可变部署身份；上线前必须验证并记录生成的镜像摘要。本地源码构建不属于发布镜像。

## 本地启动

按[开发准备](../0-foundation/001-development-setup.md)安装项目，然后授权并启动：

```bash
tgharbor login --config config.yaml
tgharbor run --config config.yaml
```

只能在可信的交互式终端执行登录。验证码和两步验证密码会直接交给 Telethon，不会通过 Bot 发送。

该命令授权的是用于处理消息链接的独立用户账号客户端，并非登录 Bot；Bot 自身使用 BotFather Token。手机号配置是可选的，因为登录命令可在本地提示输入；生成的会话会持久化到配置的 sessions 路径。

## Bot 使用

- 在 Bot 私聊中发送或转发一条媒体消息。
- 发送一条 `https://t.me/<name>/<id>` 或 `https://t.me/c/<id>/<id>` 链接。
- 使用 `/status` 查看 pending/running 任务，用 `/status <job-id>` 查看单个任务，并用 `/history [page]` 查看终态历史。
- 使用 `/cancel <job-id>` 取消等待任务或请求取消运行任务。
- 使用 `/files [page]`、`/dirs [page]` 和 `/stats` 查看共享文件库。
- 使用 `/rename`、`/archive`、`/mkdir` 或完成通知按钮整理文件。所有授权用户共享该文件库，变更会审计操作者。

用户账号必须原本就能打开链接消息。服务不会抓取历史，也不会绕过 Telegram 权限。

## 备份与恢复

完整数据卷和下载卷必须作为一个恢复点。停止服务，通过一次性 Compose 容器分别创建两个归档，并安全复制本地配置：

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

恢复前验证 `SHA256SUMS`。保持服务停止，把目标空卷或可替换卷挂载后，以相同的一次性容器方式通过 `tar -xpf` 分别恢复到 `/data` 和 `/downloads`，再恢复匹配的私有配置并启动服务。不得混用不同备份中的数据库与下载内容。bind-mount 部署应归档完整宿主目录并保留所有权和权限。正常 Bot 处理前会对账运行任务、整理意图和已跟踪 legacy 输出。Schema 1 升级请遵循 [v0.2 升级与回滚指南](303-v0-2-upgrade.md)。

## 故障排查

- **用户会话未授权**：在交互式终端重新执行 `tgharbor login` 或 Compose 登录命令。
- **Unauthorized**：把发送者数字 ID 加入 `authorized_users` 或 `TGHARBOR_TELEGRAM_AUTHORIZED_USERS`，然后重启。
- **`/data` 或 `/downloads` 权限不足**：默认命名卷应确认服务使用镜像内建用户；诊断所有权后只能重建新的空卷。bind mount 应让两个宿主目录对配置的容器 UID/GID 可写。
- **启动时无法读取配置**：bind mount 的 `config.yaml` 必须对 UID/GID `10001` 可读。把仅宿主可读的 `chmod 600` 改为 `chmod 644 config.yaml`，或采用上述所有权/组归属方案；放宽权限前先确认文件不含密钥。
- **其他实例正在使用状态目录**：停止其他进程。不要通过删除锁文件绕过运行中的锁；所有权由操作系统锁而非文件内容确定。
- **链接消息失败**：确认用户账号能打开该消息，并且消息包含实际的 Telegram 文件。链接的自动网页预览本身不是可下载媒体；即使存在该预览，受支持的 Telegram 消息链接仍会路由到链接目标。
- **中断后从零开始下载**：这是预期行为。支持任务恢复，但不保证字节级断点续传。
