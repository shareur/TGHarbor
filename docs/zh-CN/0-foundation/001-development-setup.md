# 001 — 开发准备

[English](../../0-foundation/001-development-setup.md)

在仓库根目录执行以下命令，使用 Python 3.15 或更新版本。CI 检查最低支持版本 Python 3.15。

## 环境

```bash
python3.15 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
pip install -e '.[dev]'
```

## 检查与启动

```bash
python -m ruff check .
python -m pytest -q
tgharbor
```

先在可信的交互式终端中执行一次 `tgharbor login` 创建用户会话，再用 `tgharbor run`（或直接执行 `tgharbor`）启动服务。无人值守启动时，服务不会要求输入验证码。

TGHarbor 通过 Bot 操作，但 v0.1 按设计使用两个 Telegram 客户端。Bot Token 用于认证 Bot，并下载直接发送或转发给它的媒体；手机号则只用于授权独立的用户账号会话，以打开该账号本来就有权访问的单条消息链接。手机号不是 Bot 的登录凭据；可在 `config.yaml` 中保持为 `null`，让一次性的 `tgharbor login` 命令在本地提示输入。持久化用户会话后，不会反复要求手机号和验证码。由于单条消息链接下载属于 v0.1 需求，服务当前仍要求该用户会话已授权。

## 配置

```bash
cp config.example.yaml config.yaml
```

相对路径以配置文件所在目录为基准解析。存在未知配置项或无效值时，应用将停止启动。凭据不得进入 Git，建议通过以下环境变量提供敏感信息：

- `TGHARBOR_TELEGRAM_API_ID`
- `TGHARBOR_TELEGRAM_API_HASH`
- `TGHARBOR_TELEGRAM_BOT_TOKEN`
- `TGHARBOR_TELEGRAM_PHONE`
- `TGHARBOR_TELEGRAM_AUTHORIZED_USERS`（用逗号分隔的数字用户 ID）

可通过 `TGHARBOR_CONFIG` 指定 `./config.yaml` 之外的配置文件。下载、数据库、会话、重试、并发、超时和日志设置也支持与 `config.example.yaml` 中名称对应的 `TGHARBOR_*` 环境变量覆盖。

Telegram 设置、Bot 命令、Docker 启动、备份和恢复流程见[核心下载器运维](../3-operations/301-core-downloader-operations.md)。
