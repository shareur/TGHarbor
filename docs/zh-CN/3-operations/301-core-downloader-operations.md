# 301 — 核心下载器运维

[English](../../3-operations/301-core-downloader-operations.md)

## 前置条件

创建 Telegram 应用以取得 API ID 和 API Hash，并通过 BotFather 创建 Bot Token。确定每位允许用户的数字 Telegram ID。不得提交这些值、`.env`、`config.yaml`、会话文件、数据库或下载内容。

复制并编辑示例：

```bash
cp config.example.yaml config.yaml
cp .env.example .env
cp compose.example.yaml compose.yaml
```

本地 Python 进程可把值写入已忽略的 `config.yaml`，或导出文档列出的环境变量。Compose 会自动加载已忽略的 `.env`。本地 `compose.yaml`、`config.yaml` 和 `.env` 均应保持忽略状态；Git 中只保留脱敏示例。

## 本地启动

按[开发准备](../0-foundation/001-development-setup.md)安装项目，然后授权并启动：

```bash
tgharbor login --config config.yaml
tgharbor run --config config.yaml
```

只能在可信的交互式终端执行登录。验证码和两步验证密码会直接交给 Telethon，不会通过 Bot 发送。

该命令授权的是用于处理消息链接的独立用户账号客户端，并非登录 Bot；Bot 自身使用 BotFather Token。手机号配置是可选的，因为登录命令可在本地提示输入；生成的会话会持久化到配置的 sessions 路径。

## Docker 启动

示例使用项目范围内由 Docker 管理的 `tgharbor_data` 与 `tgharbor_downloads` 命名卷，不包含固定宿主存储路径，并以内建非 root 镜像用户运行。它把本地镜像名称固定为 `tgharbor:0.2.0`，而不是浮动的次版本标签。无人值守启动前必须先创建用户会话：

```bash
chmod 600 .env config.yaml
docker compose build --pull
docker compose run --rm tgharbor login
docker compose up -d
docker compose logs -f tgharbor
```

Compose 构建使用 OCI 来源默认值，将镜像标识为未提交的本地构建。发布镜像必须改为从不可变候选构建，并按[发布安全流程](302-release-security.md#容器身份与来源)提供 revision、source 和 created 参数。本地 `tgharbor:0.2.0` 标签不是不可变部署身份；任何上线前都必须验证并记录发布镜像摘要。

Compose 以只读方式映射 `config.yaml`。数据库和两个 Telegram 会话位于数据卷，完成媒体与整理状态位于下载卷。重建容器会保留两个卷；`docker compose down -v` 会删除它们，除非数据明确可丢弃或已有备份，否则不得使用。不要把服务扩展到多个副本：TGHarbor 明确使用一个应用实例锁，并在该进程内运行多个 Worker。

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

若显式覆盖容器用户，两个宿主来源必须使用同一 UID/GID。宿主专用路径及身份覆盖只能写入被忽略的本地 Compose 文件，不得进入 `compose.example.yaml`。

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
- **其他实例正在使用状态目录**：停止其他进程。不要通过删除锁文件绕过运行中的锁；所有权由操作系统锁而非文件内容确定。
- **链接消息失败**：确认用户账号能打开该消息，并且消息包含实际的 Telegram 文件。链接的自动网页预览本身不是可下载媒体；即使存在该预览，受支持的 Telegram 消息链接仍会路由到链接目标。
- **中断后从零开始下载**：这是预期行为。支持任务恢复，但不保证字节级断点续传。
