# 002 — 功能概览

[English](../../0-foundation/002-feature-overview.md)

本文说明 TGHarbor v0.1 核心下载器已经实现的行为。代码与可重复执行的检查是最终依据；[当前限制](#当前限制)中列出的功能有意不在当前版本实现。

## 产品模型

TGHarbor 是通过 Bot 私聊操作的自托管 Telegram 下载服务。授权用户可以提交媒体、取得持久任务 ID、查看进度、取消任务并接收最终 Bot 通知。本地 SQLite 队列、异步 Worker 与安全文件系统存储使任务能够在正常进程重启后恢复。

v0.1 使用两个相互独立的 Telegram 客户端：

| 客户端 | 认证方式 | 职责 |
| --- | --- | --- |
| Bot 客户端 | BotFather Token | Bot 会话、命令、直接发送或转发的媒体及通知 |
| 用户客户端 | 一次性手机号、验证码及可选两步验证密码 | 下载单条 Telegram 消息链接引用的媒体 |

手机号不用于认证 Bot。它只在可信本地终端中授权一次独立用户会话；无人值守启动时会复用保存的会话。

## 访问与授权

- Bot 只处理私聊。来自群组或频道的消息会被忽略。
- 每次提交、状态查询和取消操作都会把发送者的 Telegram 数字用户 ID 与 `telegram.authorized_users` 比较。
- 任务按请求者隔离。授权用户即使取得其他人的任务 ID，也无法查看或取消该任务。
- 未授权私聊用户会收到 `Unauthorized.`，且不会创建任务。
- Telegram 验证码及两步验证密码只由本地 `tgharbor login` 命令接收，绝不通过 Bot 收集。

## 下载来源

### 发送给 Bot 的媒体

授权用户可在 Bot 私聊中发送或转发一条包含媒体的 Telegram 消息。TGHarbor 记录 Bot 可见的聊天与消息 ID，并通过 Bot 客户端下载媒体。

只有实际提供 Telegram 文件的消息才会创建 Bot 媒体任务。不含文件的网页预览不是可下载媒体。不属于受支持命令的文本会优先按单条消息链接解析；格式无效时会返回原因与帮助文本。

### 单条消息链接

授权用户可提交以下任意一种且仅一条 HTTPS 链接：

- 公开聊天：`https://t.me/<username>/<message-id>`
- 私有聊天：`https://t.me/c/<internal-chat-id>/<message-id>`

同时识别 `www.t.me` 和 `telegram.me` 域名。链接必须指向一个正整数消息 ID，不得包含范围、空白、凭据、自定义端口、查询参数或片段。用户账号必须本来就能打开该消息，且消息中必须存在可下载媒体。TGHarbor 不会绕过 Telegram 权限。Telegram 可能为提交的链接附加网页预览；TGHarbor 仍会通过用户账号处理链接本身，不会把该预览误判为 Bot 可见媒体。

### 重复提交

队列根据请求者 ID、来源客户端、来源聊天和来源消息 ID，为每位请求者去重。同一来源被重复提交时会返回已有持久任务，而不是再次创建下载。

## Bot 命令与响应

| 输入 | 行为 |
| --- | --- |
| `/start` 或 `/help` | 显示可接受输入和命令帮助。 |
| `/status` | 显示该请求者最近最多 10 个任务。 |
| `/status <job-id>` | 显示一个归请求者所有的任务，或报告 ID 无效/不存在。 |
| `/cancel <job-id>` | 立即取消 pending 任务，或请求取消 running 任务。 |
| 媒体消息 | 创建或返回匹配的 Bot 会话下载任务。 |
| 消息链接 | 创建或返回匹配的用户会话下载任务。 |

提交响应包含持久 UUID 任务 ID 与当前状态。状态输出包含已下载字节；当 Telegram 提供总大小时，还会显示整数百分比。失败任务会显示脱敏错误。任务完成、失败或取消时，TGHarbor 会尽力发送私聊通知；通知失败不会改变已存储的任务结果。

## 持久任务生命周期

| 状态 | 含义与转换 |
| --- | --- |
| `pending` | 等待可用 Worker 和重试时间；可以被领取或取消。 |
| `running` | 由一个 Worker 原子领取；可以完成、失败、回到 pending 重试或响应取消。 |
| `completed` | 最终文件已提交，输出路径已持久化；终态。 |
| `failed` | 发生永久错误或已耗尽配置的尝试次数；终态。 |
| `cancelled` | pending 任务被立即取消，或 running Worker 停止并丢弃部分文件；终态。 |

SQLite 保存来源标识、请求者、状态、尝试次数、进度、重试时间、Worker 所有权、取消请求、错误、输出路径和时间戳。数据库模式带版本；遇到由更新且不受支持的模式版本创建的数据库时会拒绝打开。

一条 SQL 语句会领取最早符合条件的 pending 任务并将其唯一分配给一个 Worker。Worker 发起的状态修改还必须携带相同 Worker ID，防止过期或竞争 Worker 完成其他 Worker 的任务。

## Worker、进度、重试与取消

- 可配置的异步 Worker 池在一个应用实例中并发处理多个任务。
- 新提交会唤醒空闲 Worker，空闲轮询提供后备机制。
- 每次下载尝试都有可配置超时。
- 下载进度经过节流后写入持久队列，并在 Provider 完成后强制刷新。
- Telethon 下载任务运行时会轮询取消请求；取消后停止任务、删除 `.part` 文件，并把状态改为 `cancelled`。
- 可重试失败采用有界指数延迟：`retry_base_seconds * 2^(attempt_count - 1)`。
- Telegram 限流等待时间长于计算所得延迟时，以限流等待为准。
- Telegram 限流、服务端错误、超时、操作系统 I/O 错误及未知适配器失败可重试；无效或不可访问来源以及不含媒体的消息属于永久失败。
- 达到 `downloads.max_attempts` 后停止重试；永久失败不会消耗后续无意义尝试。
- Worker 优雅停止时会丢弃当前部分文件，并把符合条件的任务交回持久重试流程。

返回给用户及日志的错误会被规范化并限制长度；不会有意暴露第三方原始异常详情或凭据。

## 重启与崩溃恢复

TGHarbor 启动时会对遗留在 `running` 状态的任务进行对账：

- 已持久化取消请求的任务变为 `cancelled`。
- 未达到尝试上限的任务立即回到 `pending`。
- 已达到尝试上限的任务变为 `failed`。

恢复以任务为单位而非字节为单位：中断的 `.part` 数据会被丢弃，下一次尝试通常从零开始。如果进程在最终原子重命名之后、数据库完成更新之前停止，替代 Worker 会识别已完成的普通文件，不重新下载即可把任务标为完成。

## 本地存储

- 每个任务都写入配置下载根目录下以任务 UUID 命名的目录。
- Telegram 文件名会进行 Unicode 规范化；路径分隔符和控制字符会被替换；空名称、点名称等不安全名称回退为 `download.bin`；UTF-8 名称限制为 180 字节，并尽量保留扩展名。
- 解析后的路径必须保持在下载根目录内，并且正好位于一层任务目录中。
- 下载根目录、任务目录、临时文件及完成文件必须是真实且类型符合预期的文件系统对象；拒绝符号链接目标。
- 下载先写入隐藏 `.part` 文件。TGHarbor 同步文件，原子替换最终路径，再同步所在目录，然后才持久化完成状态。
- 失败、取消、超时及中断尝试会在安全时删除临时文件；恢复对账不会覆盖完成文件。

## 配置

TGHarbor 加载严格 YAML，应用受支持的环境变量覆盖，拒绝未知键，并在必填值缺失或无效时停止。相对路径以 YAML 文件所在目录为基准解析。秘密数据类字段不会出现在对象表示中。

### Telegram 设置

| YAML 设置 | 环境变量覆盖 | 必填 | 用途 |
| --- | --- | --- | --- |
| `telegram.api_id` | `TGHARBOR_TELEGRAM_API_ID` | 是 | 两个客户端共同使用的 Telegram 应用数字 ID。 |
| `telegram.api_hash` | `TGHARBOR_TELEGRAM_API_HASH` | 是 | 两个客户端共同使用的 Telegram 应用秘密。 |
| `telegram.bot_token` | `TGHARBOR_TELEGRAM_BOT_TOKEN` | 是 | Bot 客户端使用的 BotFather Token。 |
| `telegram.phone` | `TGHARBOR_TELEGRAM_PHONE` | 否 | 用户账号登录输入；省略时登录命令会提示输入。 |
| `telegram.authorized_users` | `TGHARBOR_TELEGRAM_AUTHORIZED_USERS` | 是 | 非空正整数用户 ID 列表；环境变量使用逗号分隔。 |

### 运行设置

| YAML 设置 | 默认值 | 环境变量覆盖 |
| --- | --- | --- |
| `downloads.path` | `./downloads` | `TGHARBOR_DOWNLOADS_PATH` |
| `downloads.concurrency` | `2` | `TGHARBOR_DOWNLOADS_CONCURRENCY` |
| `downloads.timeout_seconds` | `3600` | `TGHARBOR_DOWNLOADS_TIMEOUT_SECONDS` |
| `downloads.max_attempts` | `3` | `TGHARBOR_DOWNLOADS_MAX_ATTEMPTS` |
| `downloads.retry_base_seconds` | `5` | `TGHARBOR_DOWNLOADS_RETRY_BASE_SECONDS` |
| `database.path` | `./data/tgharbor.db` | `TGHARBOR_DATABASE_PATH` |
| `sessions.path` | `./data/sessions` | `TGHARBOR_SESSIONS_PATH` |
| `logging.level` | `INFO` | `TGHARBOR_LOG_LEVEL` |

正数配置拒绝零和负数。日志级别只接受 `CRITICAL`、`ERROR`、`WARNING`、`INFO` 或 `DEBUG`。`TGHARBOR_CONFIG` 可选择 `./config.yaml` 以外的 YAML 文件。

## 命令行界面

| 命令 | 行为 |
| --- | --- |
| `tgharbor login [--config PATH]` | 交互式授权并持久化用户账号会话。 |
| `tgharbor run [--config PATH]` | 启动队列恢复、两个 Telegram 客户端、Worker 与 Bot 处理。 |
| `tgharbor [--config PATH]` | 启动服务；`run` 是默认命令。 |

无人值守启动绝不会提示输入 Telegram 验证码。用户会话尚未授权时，服务会给出可操作错误并停止。

## 运行与部署

- 要求 Python 3.15 或更高版本；CI 检查最低 Python 3.15 运行时。
- 应用持有数据库旁的非阻塞文件锁，拒绝第二个使用相同状态目录的 TGHarbor 进程。
- 镜像以非 root 用户运行，Compose 使用类似 `tini` 的初始化处理。
- Compose 支持选择宿主 UID/GID、可选加载 `.env`、只读 YAML 配置、持久状态/会话存储、持久下载、`no-new-privileges`、重启策略与优雅停止期限。
- Compose 把运行路径覆盖为 `/data/tgharbor.db`、`/data/sessions` 和 `/downloads`。所提供的命名卷使用 `compose.yaml` 中配置的宿主绑定来源。
- 配置、SQLite 数据库及旁文件、Telegram 会话和下载的用户内容均排除在 Git 外；经过脱敏的示例文件保持跟踪。

安装、登录、Docker 命令、宿主目录所有权、备份、恢复及故障排查见[核心下载器运维指南](../3-operations/301-core-downloader-operations.md)。

## 安全与数据边界

- 请求进入队列操作前先执行授权检查。
- Telegram 访问仅限 Bot 或已登录用户账号本来就能读取的内容；不尝试绕过权限。
- 任务查询和取消包含请求者所有权检查。
- 凭据、会话文件、运行数据库、日志及下载内容均被 Git 忽略。运维者仍须保护 `.env`、`config.yaml`、会话存储、备份和宿主权限。
- 配置及第三方错误不会渲染秘密值。
- 存储路径、文件名、文件类型和符号链接检查用于保护配置的下载根目录。
- 尽力发送的通知与持久任务状态相隔离。

## 持久数据与备份范围

正确挂载时，以下数据会在正常重建容器后保留：

- SQLite 数据库：任务、状态、进度、重试、所有权及输出路径。
- Telegram 会话目录：Bot 与已授权用户会话。
- 下载目录：按任务 ID 组织的完成媒体。

执行文件系统级备份前应停止服务，保留所有权与路径，并把以上三个位置及本地配置都视为敏感数据。详细步骤见[运维指南](../3-operations/301-core-downloader-operations.md#备份与恢复)。

## 当前限制

v0.1 有意不提供：

- 频道或聊天历史抓取。
- 消息范围、显式相册批次或批量导入。
- 绕过 Telegram 权限。
- 纯 Bot 运行模式；已选择的单条消息链接功能使当前启动必须具备已授权用户会话。
- 对中断媒体的可靠字节级断点续传。
- 多应用副本或分布式 Worker；应使用单进程及其内部异步 Worker 池。
- 云端/对象存储后端；当前只实现本地文件系统存储。
- Web UI、REST API、公开 CLI 任务管理接口或调度器。
- 自动归档索引、搜索、标签、按文件内容去重或保留策略。
- 在没有运维者自有凭据和 Docker 守护进程时完成真实 Telegram 或 Docker 主机验收；自动化测试使用伪造 Telegram 客户端。

## 相关文档

- [开发准备](001-development-setup.md)
- [系统架构概览](../1-architecture/101-system-overview.md)
- [v0.1 实现与验收](../2-implementation/201-v0-1-core-downloader.md)
- [核心下载器运维](../3-operations/301-core-downloader-operations.md)
