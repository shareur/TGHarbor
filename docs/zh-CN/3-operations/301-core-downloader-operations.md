# 301 — 核心下载器运维

[English](../../3-operations/301-core-downloader-operations.md)

## 前置条件

创建 Telegram 应用以取得 API ID 和 API Hash，并通过 BotFather 创建 Bot Token。确定每位允许用户的数字 Telegram ID。不得提交这些值、`.env`、`config.yaml`、会话文件、数据库或下载内容。

复制并编辑示例：

```bash
cp config.example.yaml config.yaml
cp .env.example .env
mkdir -p data downloads
```

本地 Python 进程可把值写入已忽略的 `config.yaml`，或导出文档列出的环境变量。Compose 会自动加载已忽略的 `.env`。在 Linux 上，把 `TGHARBOR_UID` 和 `TGHARBOR_GID` 设置为 `data/` 与 `downloads/` 所有者的 UID/GID。

## 本地启动

按[开发准备](../0-foundation/001-development-setup.md)安装项目，然后授权并启动：

```bash
tgharbor login --config config.yaml
tgharbor run --config config.yaml
```

只能在可信的交互式终端执行登录。验证码和两步验证密码会直接交给 Telethon，不会通过 Bot 发送。

该命令授权的是用于处理消息链接的独立用户账号客户端，并非登录 Bot；Bot 自身使用 BotFather Token。手机号配置是可选的，因为登录命令可在本地提示输入；生成的会话会持久化到配置的 sessions 路径。

## Docker 启动

所提供的 Compose 文件允许运维者自行选择应用目录，但其绑定宿主目录的命名卷固定使用 `/data/tgharbor/data` 保存数据库和 Telegram 会话，并使用 `/data/tgharbor/downloads` 保存完成媒体。标准 Ubuntu 布局把应用放在 `/opt/TGHarbor`，并按 `.env` 中配置的 UID/GID 准备持久目录：

```bash
sudo install -d -o 1000 -g 1000 -m 0750 \
  /opt/TGHarbor \
  /data/tgharbor/data \
  /data/tgharbor/downloads
```

如果 `TGHARBOR_UID:TGHARBOR_GID` 使用其他身份，请替换 `1000:1000`。无人值守服务启动前必须先创建用户会话：

```bash
cd /opt/TGHarbor
chmod 600 .env config.yaml
docker compose build --pull
docker compose run --rm tgharbor login
sudo chmod 600 /data/tgharbor/data/sessions/*.session
docker compose up -d
docker compose logs -f tgharbor
```

Compose 以只读方式映射 `config.yaml`，并通过命名卷以读写方式挂载两个 `/data/tgharbor` 宿主来源。卷已禁用首次复制，避免第一次使用时把宿主目录所有权替换为镜像用户的 UID/GID。数据库和两个 Telegram 会话位于 `/data/tgharbor/data` 下；重建容器不会删除它们。不要把服务扩展到多个副本：v0.1 明确使用一个应用实例锁，并在该进程内运行多个 Worker。

## Bot 使用

- 在 Bot 私聊中发送或转发一条媒体消息。
- 发送一条 `https://t.me/<name>/<id>` 或 `https://t.me/c/<id>/<id>` 链接。
- 使用 `/status` 查看近期任务，或用 `/status <job-id>` 查看单个任务。
- 使用 `/cancel <job-id>` 取消等待任务或请求取消运行任务。

用户账号必须原本就能打开链接消息。服务不会抓取历史，也不会绕过 Telegram 权限。

## 备份与恢复

执行文件系统级备份前先停止服务：

```bash
docker compose stop tgharbor
```

安全备份 `config.yaml`、`data/` 和 `downloads/`；其中包含秘密和用户内容。按相同所有权和路径恢复后启动服务。启动时会对运行任务进行对账：可重试任务回到 pending，耗尽任务失败，已持久化的取消请求进入 cancelled。

## 故障排查

- **用户会话未授权**：在交互式终端重新执行 `tgharbor login` 或 Compose 登录命令。
- **Unauthorized**：把发送者数字 ID 加入 `authorized_users` 或 `TGHARBOR_TELEGRAM_AUTHORIZED_USERS`，然后重启。
- **`/data` 或 `/downloads` 权限不足**：让宿主目录对 `TGHARBOR_UID:TGHARBOR_GID` 可写。
- **其他实例正在使用状态目录**：停止其他进程。不要通过删除锁文件绕过运行中的锁；所有权由操作系统锁而非文件内容确定。
- **链接消息失败**：确认用户账号能打开该消息，并且消息包含实际的 Telegram 文件。链接的自动网页预览本身不是可下载媒体；即使存在该预览，受支持的 Telegram 消息链接仍会路由到链接目标。
- **中断后从零开始下载**：这是 v0.1 预期行为。支持任务恢复，但不保证字节级断点续传。
