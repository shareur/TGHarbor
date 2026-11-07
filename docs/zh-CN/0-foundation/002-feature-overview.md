# 002 — 功能概览

[English](../../0-foundation/002-feature-overview.md)

本文说明当前 TGHarbor v0.2 代码库已经实现的行为。代码与可重复执行的检查是最终依据；[当前限制](#当前限制)中的功能有意不在当前版本实现。

## 产品模型

TGHarbor 是通过私有 Bot 会话操作的自托管 Telegram 下载与共享归档管理工具。本地 SQLite 队列、异步 Worker 和文件系统存储可让工作在正常进程重启后继续。

两个相互独立的 Telegram 客户端承担不同职责：

| 客户端 | 认证方式 | 职责 |
| --- | --- | --- |
| Bot 客户端 | BotFather Token | Bot 私聊、命令、直接发送/转发的媒体及通知 |
| 用户客户端 | 一次性手机号、验证码及可选两步验证密码 | 下载单条 Telegram 消息链接引用的媒体 |

手机号不用于认证 Bot。它只在可信本地终端中授权一次独立用户会话；无人值守启动会复用保存的会话，绝不会通过 Bot 索取验证码。

## 访问与授权

- Bot 只处理私聊；群组和频道消息会被忽略。
- 每项操作都会把发送者的 Telegram 数字用户 ID 与 `telegram.authorized_users` 比较。
- 任务、状态、历史及取消按请求者隔离；授权用户也不能查看或取消他人的任务。
- 完成文件目录由所有授权用户共享。任一授权用户都可列出、重命名、归档或新建归档目录。
- 重命名、归档及创建目录的审计行记录实际 Telegram 操作者 ID。共享文件库不是多租户隔离边界。

## 用户流程

### 提交媒体

授权用户可以向 Bot 发送或转发一条包含 Telegram 文件的消息，也可提交且仅提交一条受支持的消息链接：

- 公开聊天：`https://t.me/<username>/<message-id>`
- 私有聊天：`https://t.me/c/<internal-chat-id>/<message-id>`

用户账号必须本来就能访问链接消息。TGHarbor 不抓取历史，也不绕过 Telegram 权限。即使 Telegram 给 Bot 消息附加了自动网页预览，提交的链接仍通过用户客户端处理。只有实际 Telegram 文件才会作为直接 Bot 媒体接受。

队列按请求者对来源去重；重复提交同一来源会返回已有持久任务，而不是创建第二个任务。

### 查看与控制任务

`/status` 只列出请求者自己的 pending 和 running 任务；`/history [page]` 列出该请求者的 completed、failed 和 cancelled 任务。`/status <id>` 与 `/cancel <id>` 可使用完整 UUID，或请求者范围内唯一的短前缀。

任务输出包含短 ID、有界可读描述、原始或保留文件名、易读大小、状态、已下载字节，以及已知总大小时的百分比。失败任务只暴露规范化且长度受限的错误。

### 整理完成文件

完成下载会进入共享 Inbox。完成通知提供可持久化的保留、重命名、归档、重命名并归档操作。现有归档目录可通过按钮选择，也可用文本输入和安全的新建目录作为后备。待处理交互可跨重启继续，并在 24 小时后过期。

`/files [page]`、`/dirs [page]` 和 `/stats` 用于查看共享文件库；`/rename`、`/archive` 与 `/mkdir` 提供命令形式的整理操作。任何操作都不会静默覆盖已有文件。准确语法和权限范围见 [Bot 命令参考](003-command-reference.md)。

## 元数据与隐私

SQLite 保存任务来源标识、请求者与操作者 ID、状态、进度、路径、原始/当前文件名、MIME 类型、大小，以及经过空白规范化且最多 160 个 Unicode 字符的 Telegram 消息摘要。TGHarbor 不会有意持久化完整消息正文或原始 Telegram 对象。Telegram 会话、数据库、审计历史、交互状态及下载文件都属于运维者敏感数据。详情见[隐私与数据保留](004-privacy-and-retention.md)。

## 持久任务生命周期

| 状态 | 含义与转换 |
| --- | --- |
| `pending` | 等待可用 Worker 和重试时间；可以被领取或取消。 |
| `running` | 由一个 Worker 原子领取；可以完成、失败、重试或响应取消。 |
| `completed` | 最终文件与目录记录已经提交；终态。 |
| `failed` | 发生永久错误或耗尽尝试次数；终态。 |
| `cancelled` | pending 工作被取消，或 running Worker 停止并丢弃部分文件；终态。 |

一条 SQL 语句领取最早符合条件的任务。Worker 更新必须同时匹配任务 ID 与 Worker ID，避免过期 Worker 完成其他 Worker 的任务。可重试失败使用有界指数延迟；更长的 Telegram 限流等待会被遵守。不保证字节级断点续传：中断的部分数据会被丢弃，符合条件的任务会重新尝试。

启动时，已持久化的取消请求变为 cancelled，符合条件的中断任务回到 pending，耗尽尝试次数的任务变为 failed。持久交互在过期前仍可继续使用。服务正常启动前会对账文件系统整理意图，从而恢复文件移动与数据库更新之间发生的崩溃。

## 本地存储与目录

配置的下载根目录采用以下布局：

```text
downloads/
├── inbox/
├── archive/
└── .partial/
```

- Worker 写入前先在 SQLite 中预留不会冲突的 Inbox 名称。
- 下载字节和崩溃恢复状态保存在隐藏的单任务 partial 区域。
- 完成流程先执行同步且不覆盖的文件系统操作，再在 SQLite 中提交任务与共享目录记录。
- 重命名和归档会验证相对路径，拒绝符号链接及异常文件类型，确保路径位于下载根内，并在目标重名时追加短任务 ID。
- 启动时逐个把已跟踪的 v0.1 UUID 目录输出对账至 Inbox；有歧义、已变化、不安全或冲突的文件会保留给运维者检查。

数据库目录是完成文件的权威索引；文件系统和数据库必须作为一个一致性单元一起备份和恢复。

## 配置与部署

TGHarbor 要求 Python 3.15 或更高版本。严格 YAML 配置定义 Telegram 凭据与授权用户、下载行为、数据库/会话路径和日志级别；受支持的环境变量可覆盖配置。未知或无效值会阻止启动，秘密字段不会出现在对象表示中。

`tgharbor login` 在可信终端授权用户会话；`tgharbor run` 启动迁移/恢复、Telegram 客户端、Worker 和 Bot 处理。数据库旁的进程锁会阻止第二个应用实例使用同一状态。所提供容器以非 root 用户运行；不要扩展到多个副本。

完整配置和启动步骤见[开发准备](001-development-setup.md)及[运维指南](../3-operations/301-core-downloader-operations.md)。

## 持久数据与备份范围

运维者必须保护和备份：

- SQLite 数据库及旁文件：任务、目录、审计、交互和路径。
- 完整下载根目录：Inbox、Archive、partial/恢复状态及用户文件。
- Telegram 会话和本地配置。

文件系统级备份前先停止服务。数据库与下载根目录必须放在同一个恢复点；混用升级前与迁移后的状态可能导致文件失去关联或被错误识别。遵循 [v0.2 升级与回滚指南](../3-operations/303-v0-2-upgrade.md)。

## 当前限制

当前版本有意不提供：

- 频道/聊天历史抓取、消息范围或批量导入。
- 绕过 Telegram 权限。
- 纯 Bot 运行模式；单条消息链接需要已授权用户会话。
- 可靠字节级断点续传。
- 多应用副本或分布式 Worker。
- 云端/对象存储；当前只实现本地文件系统。
- Web UI、REST API、公开 CLI 管理 API 或调度器。
- 内容搜索、标签、按内容哈希去重或自动保留/删除。
- 在没有运维者自有凭据和基础设施时完成真实 Telegram、镜像、迁移或主机验收；自动化测试使用伪造 Telegram 客户端。

## 相关文档

- [Bot 命令参考](003-command-reference.md)
- [隐私与数据保留](004-privacy-and-retention.md)
- [系统架构概览](../1-architecture/101-system-overview.md)
- [文件目录与交互](../1-architecture/102-file-catalog-and-interactions.md)
- [v0.2 实现与验收](../2-implementation/202-v0-2-library-workflow.md)
- [核心下载器运维](../3-operations/301-core-downloader-operations.md)
