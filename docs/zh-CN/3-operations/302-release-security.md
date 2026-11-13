# 302 — 发布安全

[English](../../3-operations/302-release-security.md)

Telegram Bot Token、API Hash、手机号、会话文件、本地配置、数据库、日志、下载内容、消息摘要和私有部署标识都属于非公开数据。

## 首次公开发布前

1. 使用专用密钥扫描器检查候选已跟踪路径、工作树、暂存区、忽略文件、所有本地和远端 Git 引用，以及候选 `git archive`。
2. 在不输出值的前提下，将已知本地凭据值与 Git 对象比较；即使文件后来删除，匹配也必须轮换凭据。
3. 确认远端 URL 没有内嵌凭据。
4. 从精确候选提交构建 `git archive`、sdist 和 wheel。确认候选归档只包含预期的已跟踪路径，然后解压并按下方制品契约扫描每个载荷。
5. 检查 Docker 构建上下文允许清单，然后构建容器并检查其最终文件系统、配置/历史及每个镜像层，确认没有已发布项目树之外或未跟踪的材料、配置、会话、数据库、下载内容和已知凭据值。
6. 检查生成的 Compose 输出，但不得公开展开后的环境变量值。
7. 针对候选归档验证公开文档链接；若链接指向已发布项目树之外或未跟踪的材料则失败。运行 Ruff、格式检查、测试、依赖审计、文档链接验证和 Gitleaks。解决命中，或记录范围严格的误报允许项。
8. 接受外部贡献前启用仓库密钥扫描、push protection、依赖告警和分支保护。

## 发布制品

包选择属于双层安全边界。`pyproject.toml` 中的 Hatch 选择器把运行时包输入限定为 `src/tgharbor/**/*.py` 下的 Python 源码路径，但选择器并不知道某个 Python 模块是否经过评审。独立的准确制品成员安全门强制制品与显式评审的运行时清单相等，并拒绝未知 Python 模块；两层缺一不可。任意文本、JSON、YAML、归档及其他包数据都不是运行时载荷。sdist 还只允许 `pyproject.toml`、`README.md`、`LICENSE`、`CHANGELOG.md`、公开构建辅助文件 `hatch_build.py` 以及构建器生成的 `PKG-INFO`。该辅助文件移除 Hatchling 强制加入的 VCS ignore 元数据，并被打包以便解压后的 sdist 能重新构建两类制品。wheel 把 `src/tgharbor/**` 映射为 `tgharbor/**`，并且只允许显式评审的 Python 清单、`.dist-info` 元数据和许可证。

发布构建不得禁用必需的 Hatch 构建钩子。被修改的环境能够禁用或更改钩子，因此仅靠后端配置不是强制执行点；准确制品成员安全门必须拒绝 `.gitignore`、未知 Python 模块、重复成员或其他任何额外路径。每次有意新增运行时模块，都必须评审并显式更新清单测试。

针对准确候选提交：

1. 把 `git archive`、sdist 和 wheel 构建到临时目录。不得从脏工作区或其他提交构建。
2. 解压每个制品并断言完整成员清单，而不只是检查已知坏名称不存在。必须拒绝 `.env`、真实或示例配置与 Compose 文件、已发布项目树之外或未跟踪的材料、测试、完整文档、会话、数据库、日志、下载数据、重复成员、绝对或路径穿越路径，以及符号链接或硬链接成员。
3. 验证发行名称、版本和 `Requires-Python`，并确认 wheel 可在全新临时环境中安装并导入预期版本。
4. 在发布记录中记录制品文件名、大小、SHA-256 和版本。需要新制品时，必须从同一不可变候选重新构建并比较哈希。
5. 保持 `tests/test_distribution_artifacts.py` 通过；它会直接构建制品，从解压后的 sdist 重新构建两类制品，安装两条 wheel 路径，并覆盖中性文件、未知 Python 文件、本地根载荷、禁用钩子、重复成员、路径和链接等对抗场景。

在命令尚未针对准确候选实际运行前，不得声称制品或安全门已通过。Git ignore 和 Docker ignore 文件不是包安全控制。

## 容器身份与来源

可移植 Compose 示例直接拉取已发布的 Docker Hub 镜像 `shareur/tgharbor:0.2.1`，不在本地构建。`0.2.1` 发布记录位于 [Docker Hub 仓库](https://hub.docker.com/r/shareur/tgharbor)，别名 `0.2.1`、`0.2`、`latest` 和 `sha-13c5bc7645b9` 均指向 `linux/amd64` 与 `linux/arm64` 的多平台镜像索引 `sha256:b40996ef4938f8545e5eaee86c91fc57b3d27f9b087353d02a97ae7548c5f560`。已发布的 `v0.2.1` 发布记录标明该索引由源码 revision `13c5bc7645b9ba27ea9837be3697fa1388ffb44d` 构建，来源为 `https://github.com/shareur/TGHarbor`。标签仍是可变别名，不能取代已记录的镜像摘要。稳定别名只会在对应 tag-push `CI` 运行成功后发布，并由发布工作流记录 Buildx provenance 与 SBOM。

Dockerfile 提供安全的本地默认值：版本 `0.2.1`、revision `uncommitted`、source `local`，created 时间戳为 Unix epoch。这些 revision、source 与 created 默认值表示非发布构建，发布镜像禁止使用。

拉取发布镜像后，通过比较解析后的摘要和 OCI 标签核对发布记录：

```bash
docker image inspect shareur/tgharbor:0.2.1 \
  --format '{{index .RepoDigests 0}}'
docker image inspect shareur/tgharbor:0.2.1 \
  --format '{{json .Config.Labels}}'
```

`RepoDigests` 条目必须解析到已记录的索引摘要。要求 `org.opencontainers.image.version=0.2.1`、`org.opencontainers.image.revision=13c5bc7645b9ba27ea9837be3697fa1388ffb44d` 和 `org.opencontainers.image.source=https://github.com/shareur/TGHarbor`。仅有标签别名不构成来源证明。

发布镜像必须从干净的不可变候选构建。把 `TGHARBOR_IMAGE_SOURCE` 设为已验证的项目源码 URL，不得硬编码其他源码 URL；把 `TGHARBOR_IMAGE_CREATED` 一次性设为 RFC 3339 UTC 格式的候选构建时间。必须提供完整候选提交：

```bash
CANDIDATE_COMMIT="$(git rev-parse HEAD)"
test -z "$(git status --porcelain)"
test -n "$TGHARBOR_IMAGE_SOURCE"
test -n "$TGHARBOR_IMAGE_CREATED"
docker build --pull \
  --build-arg TGHARBOR_IMAGE_VERSION=0.2.1 \
  --build-arg TGHARBOR_IMAGE_REVISION="$CANDIDATE_COMMIT" \
  --build-arg TGHARBOR_IMAGE_SOURCE="$TGHARBOR_IMAGE_SOURCE" \
  --build-arg TGHARBOR_IMAGE_CREATED="$TGHARBOR_IMAGE_CREATED" \
  --tag tgharbor:0.2.1 .
```

检查生成的 OCI 标签，并要求 title 为 `TGHarbor`、version 为 `0.2.1`、revision 为准确候选提交、source 为已验证公共来源、license 为 `Apache-2.0`，created 为所提供时间戳。把镜像摘要与标签值一同记录，再运行最终文件系统、配置/历史和逐层扫描。必须从不可变候选重新构建发布镜像，不得依赖可变标签。Docker 不可用时，实际容器构建仍是未通过状态；静态 Dockerfile 测试不能替代候选安全门。完成此源码/容器发布安全门不代表批准生产上线；生产上线仍需独立的恢复与部署验收。

凭据或会话可能泄露时必须先撤销或轮换。重写历史不能让仍有效的凭据变安全。与所有仓库用户协调历史重写，并在完成后重新扫描所有引用。

## 仅源码的 0.2.2 维护发布

`v0.2.2` 是仅涉及格式化的源码发布。它对发布校验脚本和发布元数据测试应用 Ruff 格式化，使公开发布的源码快照通过仓库级格式检查，并把项目版本与运行时版本元数据对齐到 `0.2.2`。它不改变运行时行为、配置、接口或已记录的行为。

`0.2.2` 不发布容器镜像。已记录的 `0.2.1` 镜像及其 `0.2`、`latest`、`sha-13c5bc7645b9` 别名仍是当前镜像发布，上述索引摘要保持不变，Compose 说明和镜像示例继续引用已发布的 `0.2.1` 镜像。不得声称为 `0.2.2` 发布了镜像，不得把已有镜像重新标记为 `0.2.2`，也不得把 `v0.2.2` 标签当作容器来源证明。

Docker 发布工作流会在登录镜像仓库前重新校验远端标签、检出源码的版本元数据，以及所有目标 Docker Hub 标签均不存在。`0.2` 和 `latest` 已被已记录的 `0.2.1` 镜像占用，因此 `v0.2.2` 标签运行会拒绝覆盖或移动它们，并在推送前校验阶段失败。该拒绝是仅源码发布的预期结果：它既不是镜像发布成功，也不是需要修复的失败；不得为使其通过而移动不可变的 `0.2.1` 镜像标签。

## 持续控制

CI 使用只读仓库权限，检出完整历史，将 Action 固定到不可变提交 SHA，并运行依赖审计与密钥扫描。Dependabot 每周为 Python、Action 和 Docker 依赖创建数量受限的更新。

密钥扫描把固定版本 Gitleaks CLI `8.30.1` 安装到 `$RUNNER_TEMP`，不再运行市场 Action；解压前校验官方 `linux_x64` 归档的 SHA-256 `551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb`，且不向扫描器传递任何 Token 或 License 密钥。扫描对象是检出仓库的完整可达历史，而不是 push、Pull Request 或 tag 事件范围，因此初始分支推送、普通推送、Pull Request 与 tag 推送都经过同一完整历史安全门。未检出提交或检出为浅克隆时，扫描拒绝报告干净结果。命中输出执行 100% 脱敏，不使用横幅或颜色，并使用退出码 `2`；命中以密钥事件使任务失败，其他任何非零退出都报告为扫描器运行故障，绝不能被描述为扫描干净。

CI 不得上传宽泛的工作区、构建上下文或历史归档作为制品。只发布已为候选识别并验证的精确发布制品。

不得把密钥、会话文件、消息内容、私有链接、数据库行或原始诊断对象粘贴到 Issue 或 PR。维护者公开联系方式后，安全报告应使用 `SECURITY.md` 中的保密报告流程。

## 发布记录

在发布记录中记录扫描器版本及其固定归档校验和、引用数、对象数、镜像摘要、候选提交、制品名称、大小、SHA-256、版本、命令、结果、误报判断和轮换情况。不得记录凭据值、私有主机名、账号 ID 或用户内容路径。
