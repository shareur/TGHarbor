# 103 — Telegram 交互体验

[English](../../1-architecture/103-telegram-interaction-ux.md)

本文定义整合后的 Telegram 交互契约：完成操作的单卡片编辑优先工作流、单次发送回退、消息与按钮模板、尽力注册的命令菜单、单活动文本工作流规则、路由优先级、隐私边界与验收检查。它补充 [102 — 文件目录与交互](102-file-catalog-and-interactions.md)。

## 单卡片编辑优先体验

承载完成卡片的 Bot 消息是整个整理工作流的锚点：

- 保留、重命名、归档、重命名并归档以及批次 Move all 都作用于该卡片。
- 目录选择、新建目录、相对路径提示、确认、工作状态、成功、部分失败、取消和陈旧反馈都会替换同一条 Bot 消息，而不是追加一串新消息。
- 每个回调都会先确认且只确认一次，使用简短或空的 spinner 答复；该答复绝不是主要反馈。
- 卡片编辑和替换发送使用底层客户端 API，绝不使用 Telethon 的 `CallbackQuery.Event.edit` 或 `respond`，因为 Telethon 1.45 中二者都会隐式再次确认。
- 文本提示会把渲染它的 Bot 消息持久化为 `prompt_message_id`；输入文本会编辑该消息，并通过要求预期当前锚点（包括 NULL）的比较并交换重新绑定锚点。
- 每个步骤都保留简洁上下文标题：单文件为 `Job <短 ID> · <文件>`，批量为 `Album · <数量> files`。标题绝不包含宿主路径。

每个转换遵循同一形状：

```text
完成卡片 -> 目录选择 -> 提示 / 确认 -> 工作状态 -> 结果
```

保留操作会保留详细完成摘要，并追加 `Kept in Inbox. The file remains in Inbox.`，使卡片仍反映该文件。

## 回退与错误策略

- 适配器先通过客户端 `edit_message` 编辑来源消息，并把 Telegram 的“消息未修改”结果视为成功。
- 只有能证明消息无法编辑的错误（无效或缺失 ID、需要作者权限、禁止写入、编辑已过期）才会产生恰好一条内容与按钮相同的替换消息。所有保留 Back、Cancel、Retry、目录或分页控制的卡片都通过同一个辅助函数投递，该函数取得实际编辑或替换后的消息 ID，并把锚点从预期旧消息 ID 比较并交换到该 ID。
- 超时、flood wait、超长文本、模糊 RPC/网络错误和任意异常绝不发送重复反馈。它们返回一条通用安全投递失败，并让交互保持可恢复。
- 在尝试超长编辑或发送前先校验文本长度；`MessageTooLong` 绝不触发同一条超长替换发送。
- 分页回调先以原子操作消耗其准确、已授权、未过期且动作与步骤仍匹配的交互；第一个并发回调拥有该标记，其他回调在创建后继或编辑之前即被视为陈旧。后继交互只在取得所有权后创建，并以回调消息作为初始提示锚点，因此同一消息上的编辑无需重新绑定。
- 投递在取消 shielding 保护下运行，并在重新抛出待处理取消前检查实际结果，所以已经可见的后继卡片绝不会因回滚而失去按钮。替换发送会把所有后继锚点从原消息移动到替换消息，且在受 shielding 保护的关键区内完成。
- 若替换重新绑定失败，会禁用替换、删除后继行、恢复准确的原交互，并重新渲染原卡片（或发送一条重试卡片并绑定），使恢复后的来源标记出现在可见卡片中；若原交互在投递期间过期，则改为禁用原卡片。只有不存在可用后继卡片时才会回滚到来源交互，因此可见卡片绝不会指向已删除标记，恢复后的标记也绝不会缺席于可见卡片。
- 投递前发生的非回退编辑失败、超长渲染、模糊错误或取消会删除后继交互，并恢复原始交互的 ID、操作者、动作、步骤、过期时间、待定数据和提示锚点，除非原交互在投递期间已过期。
- 在提示锚点比较并交换中失败的替换消息会禁用按钮或标记为陈旧，且不影响活动卡片。
- 取消绝不会被吞掉。确认领取工作状态后，文件库变更及其到 `result-pending` 或 `retry-pending` 的立即转换构成一个取消安全关键区，包括新建目录与批次整理。工作开始后的外部取消会让操作与 pending 状态写入完成后再向上传播。提交前失败仍可恢复之前的确认或就绪重试状态与提示锚点，但已提交效果绝不会恢复重复动作。
- 最终结果投递与其成功清理一起受 shielding 保护。只有已知编辑成功或恰好一条可证明不可编辑的替换发送，才能移除终态 pending 行或把重试 pending 行变为可操作的 `ready`。发送成功返回但没有正消息标识属于模糊投递：既不会消耗终态行，也不会用旧锚点把重试行提升为 `ready`。模糊失败与未知的客户端取消会保留 pending 行以供重启恢复。
- 活跃完成卡片上的过期或未授权反馈会编辑该卡片。
- 无法解析的回调载荷、已消耗标记的重放、与当前步骤不匹配的标记，或来自非存储提示锚点消息 ID 的回调，会返回一次性反馈且不修改交互，从而保留较新的卡片和仍然有效的重试控制。
- 原始异常、宿主路径、交互 ID、消息 ID、回调载荷、名称和路径绝不会出现在聊天文本或日志中。日志只包含结果、投递方式、错误类型或安全类别和延迟类别。

## 消息与按钮模板

已接受消息：

```text
Download queued · <短 ID>
Source: Bot upload | Telegram message link
Status: pending
Next: /status <短 ID>
```

```text
Album queued · <数量> files
Status: pending
Move all appears after all files complete.
```

目录选择：

```text
Job <短 ID> · <文件>

Choose where to move this file.
Existing directories, most recently used first:
• videos
• 2025
Send a relative path as text, or use the buttons below.
[现有目录] [New directory] [Cancel]
```

没有目录时，现有目录列表替换为 `No archive directories exist yet.`。批次选择使用 `Choose where to move all files in this album.`。

新建目录提示：

```text
Job <短 ID> · <文件>

Send a relative path, for example: videos/2026. Do not start with /.
[Cancel]
```

无效目录输入保持同一步骤并编辑同一提示：

```text
Job <短 ID> · <文件>

That is not a valid relative directory. Send a relative path, for example: videos/2026. Do not start with /.
[Cancel]
```

已有目录确认：

```text
Job <短 ID> · <文件>

Confirm file organization:
Name: <名称>
Directory: <目录>
Next: Move here moves the file into this directory.
[Move here] [Back] [Cancel]
```

新目录确认使用 `Directory: <目录> (will be created)`、`Next: Create and move creates the folder and moves the file.` 和 `[Create and move] [Back] [Cancel]`。批次确认使用 `Confirm batch move:` 和相同的按钮模式。

工作与结果状态：

```text
<标题>

Working… Moving the file.
```

```text
<标题>

Completed:
[<短 ID>] <名称> · <大小>
<相对路径>
```

批次结果保留现有 `Batch move result:` 计数。存在失败或未就绪成员的结果会追加 `Some files were not moved. Choose a directory again to retry.` 和持久 `Retry move` 按钮。完全移动或全部已移动的结果会消耗工作流。失败信息绝不包含宿主路径或原始异常文本。

## 文本路由优先级

授权私聊文本更新按以下顺序路由：

1. 相册处理器恰好消耗一次分组相册。
2. `/start` 与 `/help` 始终全局可用。
3. 无任务标记的 `/cancel` 在存在活动文本工作流时取消该工作流；否则保留现有用法回复。`/cancel <job>` 保持任务取消行为。
4. 其他已识别命令（`/status`、`/history`、`/files`、`/dirs`、`/stats`、`/rename`、`/archive`、`/mkdir`）正常运行。
5. 其他任何文本，包括未知斜杠前缀文本，在能被解释为链接或上传之前优先属于活动文本工作流。
6. 只有不存在活动文本工作流时，文本才会作为 Telegram 链接或 Bot 媒体提交。

在 `await-directory` 与 `await-new-directory` 步骤，输入会作为相对归档路径校验。绝对路径、斜杠前缀值、反斜杠、空段和穿越组件会保持等待步骤、编辑同一提示，且绝不会创建任务或开始下载。在 `await-name` 步骤，文本作为待定名称处理。在确认步骤，文本会重新渲染该确认，而不是提交下载。

## 单活动文本工作流与 WORKING 领取

仓储提供无 schema 变更的操作：

```text
activate_text_workflow(interaction_id, actor_id, expected_action,
                       expected_steps, target_step, ...) -> session | None
claim_working_interaction(interaction_id, actor_id,
                          expected_action, expected_steps) -> session | None
restore_working_interaction(interaction_id, actor_id, target_step, ...) -> session | None
```

激活在一个 `BEGIN IMMEDIATE` 事务中运行。它会验证保留行存在、属于该操作者、未过期、匹配预期动作与来源步骤，并转换到请求的等待或确认状态。只有完成该转换后，同一事务才删除该操作者其他动作是 `rename`、`archive`、`rename-and-archive` 或 `batch-move`，且步骤是 `await-name`、`await-directory`、`await-new-directory`、`confirm` 或 `confirm-new-directory` 的文本工作流。分页交互和就绪完成按钮（包括无关任务）绝不会被触碰。并发激活两行会恰好留下一个活动工作流，绝不会为零，`latest_interaction_for_actor()` 会确定性地选择它。

在单文件或批次文件库/文件系统操作前，控制器会通过把 `confirm` 转换为 `working-confirm`，或把 `confirm-new-directory` 转换为 `working-confirm-new-directory` 来原子领取该确认。并发调用者中只有一个会获胜。失败方会报告 `This action is already being processed.` 或陈旧反馈，不编辑获胜方卡片，也不覆盖其结果。在文件库或文件系统数据提交前，可恢复失败或取消会恢复原确认状态或就绪重试状态，重新绑定任何替换后的 Back/Cancel/Retry 卡片并渲染其控制。整理提交后，已领取行会原子地转换为不可操作的 `result-pending` 或 `retry-pending` 状态，不再能作为确认恢复。随后最终结果投递会依据当前已授权状态重建并重试，而不重跑整理。终态 pending 行只有在已知成功投递且预期锚点与状态结算完成后才会被消耗；`retry-pending` 只有在实际消息锚点绑定后才变为可操作 `ready`，并写入新的可操作过期时间。未知或模糊的投递失败会保留 pending 状态，以供下次启动恢复。工作开始后的外层异步取消会先结算再重新抛出；文件系统与目录边界之间的 SIGKILL 或掉电不被声称为具备跨系统原子性，而依赖幂等对账与重放。

硬进程停止由启动恢复处理。一个原子恢复事务返回恢复后的会话：持久化的 `working-confirm` 与 `working-confirm-new-directory` 行回到准确的原确认并保留待定输入和锚点，旧式通用 `working` 行回到 `ready` 并清除待定输入，因此不会猜测破坏性动作。Telegram 客户端与控制器启动后、回调处理器接受事件前，适配器会尽力把每个恢复锚点编辑为安全的步骤绑定卡片。确认卡片显示待定名称与目录以及 `[Move here] [Back] [Cancel]`，新目录使用 `[Create and move] [Back] [Cancel]`。恢复的 `ready` 行显示中断动作卡片，带有与动作相应的 `Choose folder` 或 `Retry move` 控制及 `Cancel`。锚点缺失或可证明无法编辑时，会发送恰好一条替换消息并通过比较并交换绑定。操作者不再被授权时会跳过该会话且不发送聊天消息。每个会话相互隔离：失败只记录步骤、结果、类别和错误类型，不含 ID、名称或路径，绝不中止启动或改变任务状态。若编辑与发送均失败，数据库行仍保持恢复，运维者可用 `/files`、`/history`、`/dirs`、`/rename` 或 `/archive` 继续；服务不会声称可见恢复成功。持久工作状态绝不会永久搁置交互。

## 持久结果投递

已提交的整理结果保存在同一交互行中作为紧凑发件箱条目，除版本 4 外不需要 schema 变更：

```text
工作领取 --文件库结果已提交--> result-pending --已知送达 + CAS--> 已移除
工作领取 --部分批次结果------> retry-pending --已知送达 + 锚点 CAS--> ready
```

`result-pending` 保存终态单文件或完全终态批次结果，且不可操作。`retry-pending` 保存 Retry 卡片尚未确认可见的部分批次结果，在稳定前同样不可操作。两者都忽略可操作过期时间，因此已过期但未投递的结果仍可恢复。被搁置的 `working`、`working-confirm` 或 `working-confirm-new-directory` 领取即使超过原始过期时间也会被恢复，并写入新的可操作生命周期；其他已过期的可操作行保持陈旧语义。对单文件，转换还会在同一原子事务中删除该任务的其他完成动作行。

无论实时投递还是重启，终态文本都按当前已授权的任务与目录状态重建，绝不使用持久化消息文本或宿主路径。投递保持编辑优先，仅对可证明不可编辑的错误集合使用恰好一条替换发送。替换发送会先通过预期状态比较并交换绑定实际锚点，再进行清理。终态卡片没有按钮。Retry 卡片只有在已知送达且实际锚点绑定后才变为可操作 `ready`，并且该转换会写入新的可操作过期时间，因此回调既无法消耗不可见卡片，也不会在恢复后立即过期。

文件库变更与 pending 转换构成一个取消安全关键区，持久化、投递与成功清理同样受 shielding 保护。工作开始后的外部取消会让操作与 pending 写入完成后再向上传播；提交前失败仍可恢复原确认，已提交效果绝不会恢复重复确认。这只是异步取消保证：文件系统与目录边界之间发生 SIGKILL 或掉电时，依赖幂等对账与重放。重复的相同移动和重复创建已存在的目录不会产生重复审计行。

启动时先执行存储整理对账，再用新的可操作生命周期恢复工作领取，并在回调处理器启动前列出并恢复全部 pending 结果与重试行。没有固定积压上限，因此即使较早的行失败，后面的行仍会被尝试；未授权行会被跳过并保留，每行失败相互隔离，恢复只读取任务与目录状态。编辑失败或模糊错误会保留该行到下次启动。

## 命令菜单

`register_commands()` 是尽力操作，使用 Telethon `SetBotCommandsRequest` 与 `BotCommand` 条目。它注册默认作用域和全部私聊作用域（Telethon 中为 `BotCommandScopeDefault` 与 `BotCommandScopeUsers`）。准确的命令集与英文描述如下：

| 命令 | 描述 |
| --- | --- |
| `start` | Show help and usage |
| `files` | List shared files |
| `status` | Show active jobs or one job |
| `history` | Show completed, failed, or cancelled jobs |
| `dirs` | List archive directories |
| `cancel` | Cancel a job or the active prompt |
| `stats` | Show library statistics |
| `rename` | Rename a stored file |
| `archive` | Move a file into an archive directory |
| `mkdir` | Create an archive directory |
| `help` | Show help and usage |

每个作用域相互隔离失败。作用域失败只记录结果、作用域和错误类型；不会中止启动，键入命令仍完全可用。注册在 Bot 客户端启动且控制器构造之后、启动通知与 Worker 稳态之前运行。不注册按授权用户划分的作用域，因为 Telethon 需要对应的 peer/user 实体，而其可选收益不足以抵消额外失败面。

## 隐私

- 聊天反馈只使用逻辑文件库路径和受限名称。
- 提示锚点、回调载荷、交互 ID、Telegram ID、请求者 ID 和消息 ID 绝不写入日志。
- 目录按钮携带绑定所显示目录与顺序的不透明摘要；列表变化会拒绝旧选择且不产生变更。
- 跨用户、跨动作、过期和重放的标记会被拒绝，且不修改文件库或文件系统。

## 验收

- 单文件与批次完成卡片通过目录选择、新建目录、有效和无效输入、确认、工作状态和结果，在同一条消息锚点上流转。
- 每个保留 Back、Cancel、Retry、目录或分页控制并回退到替换消息的卡片都会把交互锚点重新绑定到替换 ID；点击替换卡片上的 Back 或 Retry 成功，而旧锚点属于陈旧请求。
- Back 从确认返回目录选择；Cancel 在目录选择、提示和确认中可用。
- 无效或斜杠前缀目录输入保持等待步骤、显示相对路径示例，且不创建任务。
- 保留操作保留完成摘要并说明文件仍在 Inbox。
- 已接受的单条上传和链接显示来源、pending 状态和下一步 `/status` 指引。相册接收显示文件数、初始状态，以及所有文件完成后才出现 Move all。
- 详细完成与失败摘要把 `Job: <短 ID>` 紧接标题之后，且不改变其余字段顺序。
- 部分批次结果持久化为 `retry-pending`，在同一卡片上保留重试控制，并且只有在卡片已知送达且实际锚点绑定后才变为可操作 `ready`，同时写入新的可操作过期时间。终态结果持久化为 `result-pending`，仅在已知送达后移除。重放、跨动作和跨用户标记保持陈旧且不产生变更，重试只移动仍在 Inbox 的成员。
- 并发分页回调恰好只有一个所有者：一次编辑、一组后继交互，所有失败方均为陈旧或无变更。后继以回调消息为初始锚点，同一消息编辑无需重新绑定，替换发送在受 shielding 保护的关键区内重新绑定。重新绑定失败会禁用替换并重新渲染恢复后的来源标记。每个可见分页标记都解析到持久活动行；只有不存在可用后继卡片时才回滚。
- 领取工作状态后的取消绝不会搁置交互：文件库变更与 pending 转换在一个取消安全关键区内完成，工作开始后的外部取消会先稳定操作与 pending 写入再传播；提交前失败恢复之前的确认与锚点，已提交效果保留 pending 结果。启动恢复会还原任何持久工作或 pending 状态，包括已超过原过期时间的领取，并写入新的可操作生命周期。恢复后的确认与旧式就绪卡片会被编辑为可见且绑定步骤的控制；锚点缺失或消息不可编辑时使用一条已绑定替换，失败保持隔离并可通过命令恢复。
- 最终编辑或发送失败、取消都会留下可恢复的结果行；发送成功但没有正消息标识属于模糊投递并保留该行；重启会按当前目录状态投递，且不重跑整理；模糊失败会保留该行到下次启动。
- 每个操作者只有一个活动文本工作流，开始新工作流不会影响分页和就绪完成按钮。
- 命令注册使用准确命令集与两个作用域，遵守启动顺序，并隔离作用域失败且不泄露 ID。

## 相关文档

- [102 — 文件目录与交互](102-file-catalog-and-interactions.md)
- [002 — 功能概览](../0-foundation/002-feature-overview.md)
- [003 — Bot 命令参考](../0-foundation/003-command-reference.md)
- [202 — v0.2 文件库工作流](../2-implementation/202-v0-2-library-workflow.md)
- [301 — 核心下载器运维](../3-operations/301-core-downloader-operations.md)
