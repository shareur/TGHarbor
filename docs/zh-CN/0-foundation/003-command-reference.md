# 003 — Bot 命令参考

[English](../../0-foundation/003-command-reference.md)

只有配置的 Telegram 数字用户 ID 才能在 Bot 私聊中执行命令。任务只允许请求者查看或取消；文件库由所有授权用户共享，变更操作会记录操作者 ID。

## 任务命令

| 命令 | 行为 |
| --- | --- |
| `/start`、`/help` | 显示输入方式和命令帮助。 |
| `/status` | 最多列出 10 个属于请求者的 pending/running 任务。 |
| `/status <id>` | 解析属于请求者的完整或唯一短任务 ID。 |
| `/history [page]` | 每页列出 10 个请求者终态任务，并提供持久分页按钮。 |
| `/cancel <id>` | 取消 pending 任务，或请求取消请求者自己的 running 任务。 |
| `/cancel` | 取消当前重命名/目录提示。没有活动提示时显示任务取消用法。 |

Bot 还会为默认作用域和全部私聊作用域尽力注册命令菜单，准确命令为 `start`、`files`、`status`、`history`、`dirs`、`cancel`、`stats`、`rename`、`archive`、`mkdir` 和 `help`。注册失败只记录结果、作用域和错误类型，绝不阻塞启动或键入命令。

## 共享文件库命令

| 命令 | 行为 |
| --- | --- |
| `/files [page]` | 每页列出 10 个共享文件，并提供持久分页按钮。 |
| `/dirs [page]` | 每页列出 10 个归档目录。 |
| `/stats` | 显示文件数、总大小、Inbox 数和 Archive 数。 |
| `/rename <file-id> <new-name>` | 重命名共享文件并审计操作者。 |
| `/archive <file-id> <directory>` | 把共享文件移入已有归档目录并审计操作者。 |
| `/mkdir <directory>` | 安全创建相对归档目录并审计操作者。 |

只有在命令授权范围内前缀唯一时才接受短 ID。拒绝绝对路径、点/穿越组件、符号链接和不受支持的名称；命令不会静默覆盖文件。

## 相册接收

发送 Telegram 相册时，每条媒体只入队一次，共享一个不透明批次身份并使用相册准确成员数。单条消息或受支持的消息链接仍是一成员任务；链接到相册中的一个成员不会通过抓取远程相册展开。已接受的链接/上传消息会回复 `Download queued · <短 ID>`，包含来源、pending 状态和下一步 `/status <短 ID>` 指引；相册会回复 `Album queued · <数量> files`，并说明所有文件完成后才出现 Move all。

## 完成消息与操作

成功下载始终按以下顺序和英文标签显示字段：

```text
Download Completed
Job: <短任务 ID>
File Name: <最终目录名称>
Download Folder: /download/<相对父目录>
File Size: <MiB> MB
Start Time: <服务器本地 HH:MM:SS>
End Time: <服务器本地 HH:MM:SS>
Download Time: <秒>
Download Speed: <KiB/s> KB/s
User Id: <请求者>
Origin Group: <值或 N/A>
Channel ID: <值或 N/A>
```

目录始终是逻辑 `/download/` 路径，绝不暴露宿主绝对路径。时间使用服务器本地时区；耗时与速度由持久化尝试时间戳和目录中存储的大小计算。缺失值显示 `N/A`。

该消息同时提供持久的保留、重命名、归档、重命名并归档按钮。这些操作以及批次 `Move all to new folder` 都会原地编辑来源完成卡片；只有消息无法编辑时才回退为一条替换发送。简洁的 `Job <短 ID> · <文件>` 或 `Album · <数量> files` 标题始终保持可见。目录选择会说明下一步动作，按最近使用顺序列出现有目录，并包含 `New directory` 与 `Cancel`；没有目录时显示 `No archive directories exist yet.`。新建目录提示为 `Send a relative path, for example: videos/2026. Do not start with /`。无效或斜杠前缀输入会停留在该提示并显示相同示例，绝不创建任务。已有目录确认使用 `Move here`；新目录使用 `Create and move` 并说明将创建目录。两者都提供 `Back` 与 `Cancel`。文件库操作前卡片显示 `Working…`，随后由结果替换。目录按钮携带绑定所显示目录与顺序的不透明摘要，因此排序或列表变化会把旧选择作为陈旧请求拒绝并显示刷新后的选项，而不会选择其他目录。确认后只通过 Library API 移动当前 Inbox 成员，报告已移动、跳过和失败数量且不包含宿主路径；陈旧、跨动作或跨请求者回调会被拒绝且不产生变更。存在失败或未就绪成员的结果会在同一卡片上保持就绪状态并提供持久 `Retry move` 操作；只有完全移动或全部已移动的结果才会消耗交互。名称和目录输入会写入 SQLite，直到确认、取消或 24 小时过期。每次回调都会重新检查授权、过期时间、操作者和目录状态。保留操作会保留完成摘要，并说明文件仍在 Inbox。

## 启动控制

重启恢复和 Telegram 启动完成后，每个授权用户会收到一条私有初始化消息，包含包版本、带时区的服务器本地 ISO 时间戳及该请求者的中断数量。数量大于零时附带持久的 `View interrupted tasks` 与 `Continue interrupted tasks` 按钮；为零时说明没有中断任务。查看操作按请求者隔离，不显示宿主路径或他人任务。继续操作只恢复该请求者的暂停任务并唤醒 Worker；重复查看或继续是幂等的，再次继续会报告零。

## 脱敏示例

```text
用户：/status
Bot： [1a2b3c4d] 示例视频
     状态：running
     文件：example.mp4
     大小：128.0 MiB
     进度：42% · 53.8 MiB

Bot： Download Completed
     Job: 9f8e7d6c
     File Name: conference.mp4
     Download Folder: /download/inbox
     File Size: 512.00 MB
     Start Time: 14:02:11
     End Time: 14:03:27
     Download Time: 76.00
     Download Speed: 6898.53 KB/s
     User Id: 1001
     Origin Group: N/A
     Channel ID: N/A
     [保留] [重命名] [归档] [重命名并归档]
```

示例中的 ID、名称和大小均为虚构。公开截图或缺陷报告必须采用同等程度的脱敏数据。
