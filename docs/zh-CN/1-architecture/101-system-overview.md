# 101 — 系统架构概览

[English](../../1-architecture/101-system-overview.md)

TGHarbor 将外部接口与下载领域分离。

```text
Telegram / 未来的 REST API / CLI
              |
              v
           Job API
              |
              v
           持久队列
              |
              v
           Worker
              |
              v
          Provider
              |
              v
           Storage
```

## 原则

1. Telegram 是适配器，不是应用核心。
2. 下载任务是持久化的领域对象。
3. Worker 能在重启后恢复未完成的任务。
4. Provider 封装不同来源的下载行为。
5. 存储路由独立于 Provider。
6. UI/API 层不应持有下载状态。

共享文件库路径与下载状态所有权相互独立：

```text
Telegram Bot
     |
     v
 Library API ------> SQLite 目录与审计
     |                         |
     +-------------------------+
                   |
                   v
               本地存储
```

## Telegram 适配器

TGHarbor 在相同 Provider 契约后使用两个相互独立的 Telegram 会话：

- Bot 会话接收授权用户发送或转发的媒体，并下载 Bot 可见的消息。
- 用户会话解析单条公开或私有 `https://t.me/` 消息链接。登录账号必须原本就有权访问该消息。

授权边界位于 Job API。创建、查询或取消任务前都会检查 Telegram 用户 ID，任务只能由提交者访问。Bot 只处理私聊；验证码和两步验证密码只能通过本地 `tgharbor login` 命令输入。

Library API 构成第二个授权边界。所有配置的授权用户共享完成文件的可见性和整理权限。变更操作会记录实际 Telegram 操作者 ID、重新验证目录状态，并且不会从共享文件库权限推断任务所有权。

## 队列语义

```text
pending --领取--> running --成功--> completed
   |                 |  |
   |                 |  +--永久失败/次数耗尽--> failed
   |                 +-----可重试-------------> pending（延迟）
   +--取消------------------------------------> cancelled
running --观察到取消请求---------------------> cancelled
```

SQLite 保存状态、来源引用、提交者、有界显示元数据、尝试次数、进度、下次可用时间、Worker 所有权、错误和输出路径。一条 SQL 语句领取 pending 行并把它分配给一个 Worker。Worker 更新状态时同时检查任务 ID 与 Worker ID。TGHarbor 运行一个应用实例和多个异步 Worker；数据库旁的进程锁会拒绝第二个实例。

启动时，未耗尽尝试次数的中断任务回到 pending；耗尽任务进入 failed；已持久化的取消请求进入 cancelled。这是任务级重启恢复：部分文件会被丢弃并重新下载，当前版本不保证字节级断点续传。

## 存储与完成

存储把配置下载根划分为共享 `inbox/`、共享 `archive/` 与隐藏 `.partial/`。Worker 先在 SQLite 中预留不会冲突的 Inbox 名称，再写入单任务 partial 区域；同步文件后以不覆盖语义提交，最后在同一数据库事务中完成任务和目录记录。解析路径必须位于根目录内，并拒绝符号链接及异常文件系统对象类型。

重命名和归档操作会先持久化文件系统意图，再移动文件、更新目录与操作者审计，最后删除意图。启动对账可完成安全的中断移动，或拒绝已变化及有歧义的状态。另一个一次性对账器会把已跟踪的 v0.1 UUID 目录输出移入 Inbox，且不会覆盖无关文件。

Provider 失败分为永久失败和可重试失败。可重试失败采用有上限的指数延迟，并遵守更长的 Telegram 限流等待时间。Bot 终态通知为尽力发送，通知失败不会改变已持久化的下载结果。
