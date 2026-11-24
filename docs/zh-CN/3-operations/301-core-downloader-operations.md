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

示例使用项目范围内由 Docker 管理的 `tgharbor_data` 与 `tgharbor_downloads` 命名卷，不包含固定宿主存储路径，并以内建非 root 镜像用户运行。它把本地镜像名称固定为 `tgharbor:0.2.3`，而不是浮动的次版本标签。无人值守启动前必须先创建用户会话：

```bash
chmod 600 .env config.yaml
docker compose build --pull
docker compose run --rm tgharbor login
docker compose up -d
docker compose logs -f tgharbor
```

Compose 构建使用 OCI 来源默认值，将镜像标识为未提交的本地构建。发布镜像必须改为从不可变候选构建，并按[发布安全流程](302-release-security.md#容器身份与来源)提供 revision、source 和 created 参数。本地 `tgharbor:0.2.3` 标签不是不可变部署身份；任何上线前都必须验证并记录发布镜像摘要。

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

## 日志

`logging.level` 只控制 TGHarbor 自身的日志记录，不会启用 Telethon 库的低级别输出：即使 `logging.level` 为 `INFO` 或 `DEBUG`，Telethon 日志命名空间也始终限制为 `ERROR` 或更严格级别。这可避免高流量的 update difference 消息淹没日志。这些错误记录由 Telethon 自身写入，因此该设置只控制级别，不控制 Telethon 选择输出的内容。

TGHarbor 将日志写入标准错误；Docker 通过其日志驱动一并捕获标准输出和标准错误。使用 `docker compose logs -f tgharbor` 查看，或通过本地 Python 进程的服务管理器日志查看。启动时会报告 worker 数量，正常运行不会反复输出 traceback。

当 SQLite 或其存储适配器失败时，TGHarbor 会记录脱敏的持久化诊断，而不是原始异常。记录会标明受影响的任务或 worker，并包含固定类别、异常类名，以及可用的数字 SQLite 主错误码或操作系统 `errno`。类别包括 `busy`、`open`、`io`、`disk-full`、`permission`、`read-only`、`corrupt`、`schema`、`integrity`、`adapter` 和 `database`。这些持久化诊断不会包含 SQL 文本、绑定参数、行值、文件系统或会话路径、Telegram 链接或标识、文件名、标题、Token 或原始异常消息。

处理任务时发生短暂持久化失败（`busy`、`io`、`disk-full`、`permission`、`read-only` 或 `open`），会按现有尝试次数上限和退避策略重新排队，并以 `database <category>` 形式的受限原因记录；尝试次数耗尽后照常失败。永久类别（`corrupt`、`schema`、`integrity`、`adapter` 或回退的 `database`）会直接使任务失败，而不是重复无法自行恢复的操作。发送完成通知失败只记录日志，不改变已持久化的结果。领取工作失败会记录日志，worker 继续轮询，因此短暂锁不会终止 worker；重复的相同永久失败只记录一次，直到出现不同失败或成功。同一类别、异常类和错误码的重复短暂失败会在首次出现时完整记录，之后每分钟最多记录一次并附带被抑制的数量，下一次成功迭代后会记录一条恢复信息。若因数据库本身不可用而无法记录最终状态，任务会停留在运行状态，直到启动恢复将其暂停以等待请求者显式继续，或在尝试次数耗尽后判定失败；服务无法向拒绝写入的存储强制写入终态记录，因此恢复数据库后应与请求者一起核对该任务再采信其结果。

内置 Telegram 提供方预期失败的用户可读详情，只有在与固定消息完全匹配时才会被存储并展示给请求者。其他提供方异常详情在记录或存储前只会归约为异常类名，因此第三方或适配器文本不会回显给请求者。

连接在报告 `busy` 前最多等待竞争写入者 15 秒。遇到 `busy` 或 `io` 诊断时，检查是否存在并发实例、宿主存储健康状况和可用空间；遇到 `permission` 或 `read-only` 时，检查数据目录所有权和挂载模式；遇到 `corrupt` 或 `integrity` 时，停止服务并从经验证的备份恢复，不要删除数据库。

## Bot 使用

- 在 Bot 私聊中发送或转发一条媒体消息。
- 发送一条 `https://t.me/<name>/<id>` 或 `https://t.me/c/<id>/<id>` 链接。
- 使用 `/status` 查看 pending/running 任务，用 `/status <job-id>` 查看单个任务，并用 `/history [page]` 查看终态历史。
- 使用 `/cancel <job-id>` 取消等待任务或请求取消运行任务。
- 使用 `/files [page]`、`/dirs [page]` 和 `/stats` 查看共享文件库。
- 使用 `/rename`、`/archive`、`/mkdir` 或完成通知按钮整理文件。所有授权用户共享该文件库，变更会审计操作者。

内联操作会立即且恰好一次以 spinner 确认，然后以持久聊天反馈报告每个提示、成功结果、陈旧选择或错误，使结果保留在会话中。卡片编辑使用底层客户端 API，而不是会隐式再次确认的 Telethon 回调快捷方法。完成卡片操作采用单卡片编辑优先：保留、重命名、归档、重命名并归档以及批次 Move all 会在目录选择、新建目录提示、确认、可见工作状态和结果之间编辑来源消息。只有无效、缺失、需要作者权限或编辑已过期的消息才会回退为恰好一条替换消息。所有保留 Back/Cancel/Retry/目录/分页控制的卡片都通过同一个辅助函数投递，该函数把提示锚点从预期旧消息重新绑定到实际编辑或替换后的消息 ID。超时、flood wait、超长文本、模糊错误以及发送成功但没有正消息标识只报告一条通用或安全的投递失败，不重复反馈并保留 pending 行。目录选择按最近使用顺序列出现有归档目录，并包含新建目录和取消；没有目录时会明确说明。确认步骤对已有目录提供 `Move here`，对新目录提供 `Create and move`，并带 `Back` 与 `Cancel`。单个原子领取会把 `confirm` 转换为 `working-confirm`，或把 `confirm-new-directory` 转换为 `working-confirm-new-directory`，因此重复回调会报告正在处理，而不会重复执行文件库操作。可恢复的单文件与批次失败会恢复确认或重试状态，并在替换卡片上渲染 Back/Cancel 或 Retry/Cancel 控制。领取后，文件库变更及其到 pending 的立即转换构成一个取消安全关键区，包括新建目录与批次整理；工作开始后的外部取消先稳定操作与 pending 写入再传播，提交前失败可恢复之前的确认或就绪重试状态与提示锚点，已提交效果绝不恢复重复动作。这只是异步取消保证；文件系统与目录边界之间发生 SIGKILL 或掉电时依赖幂等对账与重放。启动会用新的可操作生命周期恢复任何持久工作状态（包括已超过原过期时间的领取）并返回恢复后的会话；Telegram 启动后、回调处理前，Bot 会尽力把每个恢复锚点编辑为正确可见卡片：确认保留待定文件/目录上下文及 Confirm、Back、Cancel；新目录显示 Create and move；旧式 `working` 行成为带相应 Choose folder 或 Retry move 控制及 Cancel 的中断动作卡片。锚点缺失或可证明无法编辑时发送恰好一条替换并通过比较并交换绑定。每个会话相互隔离；失败不会中止启动、改变任务状态或阻塞其他用户。若编辑与发送均失败，恢复行仍可通过 `/files`、`/history`、`/dirs`、`/rename` 或 `/archive` 继续。无效或斜杠前缀目录输入会停留在提示并显示相对路径示例，绝不创建任务。确认等待期间发送文本会重新渲染该确认，而不会开始新的下载。每个用户只有一个活动文本工作流；激活是原子的，开始新的重命名或目录工作流不会影响分页按钮和其他完成卡片。无任务 ID 的 `/cancel` 会取消当前提示。首次 `/files` 或 `/history` 命令会发送一条新列表；“下一页”和“上一页”会编辑同一条 Bot 消息，而不是追加另一条列表。分页回调会在创建后继前原子消耗其准确、已授权、未过期且动作与步骤匹配的交互；第一个并发回调拥有它，其他回调为陈旧。后继以回调消息为初始锚点，因此原地编辑无需重新绑定；投递受 shielding 保护并在重新抛出取消前提交。回退替换会在受 shielding 保护的关键区内重新绑定所有后继锚点。若重新绑定失败，会禁用替换、删除后继、恢复准确的原始交互，并重新渲染原卡片（或发送一条重试卡片并绑定），使恢复后的来源标记可见。投递前的非回退失败或取消会删除后继并恢复准确的原始分页交互，因此旧分页仍可用（除非原交互在投递期间已过期）。较早步骤或较旧提示锚点的按钮会被报告为陈旧，且不会覆盖待定输入。分页按钮只能使用一次：重放的分页按钮属于陈旧请求且不产生变更。提示锚点通过要求预期值的比较并交换重新绑定，因此旧卡片不能替换较新卡片。

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

恢复前验证 `SHA256SUMS`。保持服务停止，把目标空卷或可替换卷挂载后，以相同的一次性容器方式通过 `tar -xpf` 分别恢复到 `/data` 和 `/downloads`，再恢复匹配的私有配置并启动服务。不得混用不同备份中的数据库与下载内容。bind-mount 部署应归档完整宿主目录并保留所有权和权限。正常 Bot 处理前会恢复整理意图和已跟踪 legacy 输出。符合条件的中断运行任务会成为暂停 pending 任务，普通领取会跳过，提交者必须通过 Job API 列出并继续；耗尽的中断任务进入 failed，已持久化取消进入 cancelled。Schema 1、2 或 3 升级请遵循 [v0.2 升级与回滚指南](303-v0-2-upgrade.md)。

恢复完成且 Telegram 启动后，服务会向每个配置的授权用户发送一条尽力发送的私有初始化消息，包含包版本、带时区的服务器本地 ISO 时间戳及该用户的中断数量。数量大于零时附带持久的 View interrupted tasks 与 Continue interrupted tasks 按钮。Worker 先于这些消息启动，因此普通 pending 任务不会被阻塞。单个用户投递失败只记录通用错误类型，不中止启动，也不影响其他用户。重复重启会保留未继续任务的暂停状态。不要直接操作 SQLite 手工构造这些控制；应使用 Bot 交互与 Job API。

恢复后的交互卡片独立于启动通知。其数据库状态已恢复，任务状态不受影响。若编辑与替换发送均失败，日志只记录 `outcome=not-visible`、步骤、安全类别和错误类型；应通过新命令或动作路径继续，而不是编辑 SQLite：使用 `/files` 或 `/history` 找到条目，再按需使用 `/rename`、`/archive`、`/dirs` 或 `/mkdir`。在卡片出现前不要声称可见恢复成功。

## 故障排查

- **用户会话未授权**：在交互式终端重新执行 `tgharbor login` 或 Compose 登录命令。
- **Unauthorized**：把发送者数字 ID 加入 `authorized_users` 或 `TGHARBOR_TELEGRAM_AUTHORIZED_USERS`，然后重启。
- **命令菜单缺失或不完整**：命令菜单注册是尽力操作。确认 Bot 已启动，并检查只记录结果、作用域和错误类型的警告；单个作用域失败不会禁用键入命令。重新连接 Telegram 客户端或重启服务会重试注册。诊断时不要把用户 ID 写入日志。
- **`/data` 或 `/downloads` 权限不足**：默认命名卷应确认服务使用镜像内建用户；诊断所有权后只能重建新的空卷。bind mount 应让两个宿主目录对配置的容器 UID/GID 可写。
- **其他实例正在使用状态目录**：停止其他进程。不要通过删除锁文件绕过运行中的锁；所有权由操作系统锁而非文件内容确定。
- **链接消息失败**：确认用户账号能打开该消息，并且消息包含实际的 Telegram 文件。链接的自动网页预览本身不是可下载媒体；即使存在该预览，受支持的 Telegram 消息链接仍会路由到链接目标。
- **文件被命名为 `telegram_<id>.bin`**：Telegram 未提供可用文件名，且其扩展名或 MIME 类型未知或不安全。之后可使用 `/rename` 或完成按钮重命名。
- **中断下载不会自动重新开始**：重启恢复后这是预期行为。符合条件的中断任务保持暂停 pending，直到提交者通过启动消息或 Job API 继续；耗尽任务进入 failed，已持久化取消保持 cancelled，且不保证字节级断点续传。
- **恢复后的确认或 Working 卡片缺失或不可操作**：交互行已恢复，但尽力编辑或替换发送失败。检查只包含步骤、类别和错误类型的 `outcome=not-visible`；日志绝不含 ID、名称或路径。使用 `/files` 或 `/history` 找到条目，再使用 `/rename`、`/archive`、`/dirs` 或 `/mkdir`。不要手工编辑或删除交互行，卡片出现前不要声称可见恢复成功。
- **重启后完成结果或重试结果卡片缺失**：文件库结果已经提交，但因最终编辑、替换发送或发送状态清理模糊而保留了 `result-pending` 或 `retry-pending` 行。替换发送成功但未返回正消息标识时只记录安全投递失败类别，并保留该行 pending。下次启动会按当前目录状态重建卡片，且不会重跑整理。只检查包含步骤与错误类型的 `outcome=not-visible` 或陈旧状态清理警告，绝不含 ID 或路径。不要手工删除该行；通过 `/files` 或 `/history` 查看实际文件状态。
- **分页卡片在更新失败后显示陈旧控制**：适配器会禁用未绑定的替换、恢复来源交互并重新渲染其标记。请使用可见的 Retry 控制；若没有卡片，重新运行 `/files` 或 `/history`，不要删除交互行。
- **没有启动初始化消息**：确认用户 ID 在 `telegram.authorized_users` 中、Bot 能发起私聊，并检查日志是否只记录通用启动通知失败。其他用户仍应收到各自通知；不要仅为强制单次投递而重启。
- **相册完成后没有 Move all**：每个成员都必须成功，且批次必须包含多个成员。部分失败或取消的相册不会提供 Move all。持久通知发件箱会在启动时及服务运行期间周期性重驱动，因此临时 Telegram 失败不需要下一次完成回调或进程重启。`sent` 通知是终态。若发送成功但未记录发送状态，重试可能再次投递通知；重试复用同一交互与回调身份，因此重放按钮保持幂等且状态最终收敛到 sent。应将其视为跨 Telegram/SQLite 边界的至少一次送达，而不是恰好一次。交互在复用时刷新，并在 24 小时无活动后过期。
- **Move all 报告跳过成员**：跳过表示已归档、目录中缺失或尚未就绪。操作仅限 Inbox 且逐文件执行；应检查报告数量，而不是删除数据库行或文件。存在失败或未就绪成员的结果会保留交互并提供 `Retry move`；若目录列表在渲染后发生变化，所选目录会作为陈旧请求拒绝且不移动任何文件，请从刷新后的列表重新选择。
