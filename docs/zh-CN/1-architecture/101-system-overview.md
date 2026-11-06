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

## v0.1 适配器

v0.1 在相同 Provider 契约后使用两个相互独立的 Telegram 会话：

- Bot 会话接收授权用户发送或转发的媒体，并下载 Bot 可见的消息。
- 用户会话解析单条公开或私有 `https://t.me/` 消息链接。登录账号必须原本就有权访问该消息。

授权边界位于 Job API。创建、查询或取消任务前都会检查 Telegram 用户 ID，任务只能由提交者访问。Bot 只处理私聊；验证码和两步验证密码只能通过本地 `tgharbor login` 命令输入。

## 队列语义

```text
pending --领取--> running --成功--> completed
   |                 |  |
   |                 |  +--永久失败/次数耗尽--> failed
   |                 +-----可重试-------------> pending（延迟）
   +--取消------------------------------------> cancelled
running --观察到取消请求---------------------> cancelled
```

SQLite 保存状态、来源引用、提交者、尝试次数、进度、下次可用时间、Worker 所有权、错误和输出路径。一条 SQL 语句领取 pending 行并把它分配给一个 Worker。Worker 更新状态时同时检查任务 ID 与 Worker ID。v0.1 运行一个应用实例和多个异步 Worker；数据库旁的进程锁会拒绝第二个实例。

启动时，未耗尽尝试次数的中断任务回到 pending；耗尽任务进入 failed；已持久化的取消请求进入 cancelled。这是任务级重启恢复：部分文件会被丢弃并重新下载，v0.1 不保证字节级断点续传。

## 存储与完成

每个任务在下载根目录下使用独立目录。文件名会被清理，解析后的路径必须位于根目录内，并拒绝符号链接目标。Worker 写入 `.part` 文件，同步文件后原子重命名，再同步目录，最后把数据库任务标记为完成。如果进程在重命名后、数据库更新前停止，替代 Worker 会识别完整文件并补齐数据库状态，不会重复下载。

Provider 失败分为永久失败和可重试失败。可重试失败采用有上限的指数延迟，并遵守更长的 Telegram 限流等待时间。Bot 终态通知为尽力发送，通知失败不会改变已持久化的下载结果。
