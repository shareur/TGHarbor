# TGHarbor

[English](../../README.md)

**可自托管、可恢复的 Telegram 下载与共享归档管理工具。**

TGHarbor 接收发送或转发给私有 Bot 的媒体，也可通过独立授权的用户账号处理该账号本来就能访问的单条 Telegram 消息链接。服务以 SQLite 持久化下载任务，把文件安全写入本地 Inbox，并允许授权用户通过 Bot 命令和内联按钮整理共享文件库。

> 项目处于早期开发阶段。升级前请备份状态；v1.0 之前配置和内部 API 可能发生变化。

## 主要功能

- Bot 接收媒体，用户会话处理单条消息链接。
- 请求者范围内的持久任务、进度、取消、重试及重启恢复。
- 最多 160 字的可读消息摘要、文件名、MIME 类型和文件大小。
- 共享 `inbox/` 与 `archive/`，支持重命名、归档和创建目录。
- 可跨进程重启继续的保留/重命名/归档交互。
- 防止路径越界、符号链接和静默覆盖的冲突安全本地存储。
- SQLite schema 1 到 2 的事务迁移及旧 UUID 目录对账。
- 非 root Python 3.15 容器部署和安全检查 CI。

## 开发

环境准备、检查、启动及配置方式见[开发准备](0-foundation/001-development-setup.md)。

## 当前状态

v0.2 共享文件库工作流已实现，并由不含真实凭据的自动化测试覆盖。真实 Telegram、容器镜像、迁移演练和部署验收仍依赖运维者自有基础设施，不能从单元测试结果推定已经通过。

## 文档导航

| 分类 | 文档 |
| --- | --- |
| 0 — 准备 | [001 — 开发准备](0-foundation/001-development-setup.md) · [002 — 功能概览](0-foundation/002-feature-overview.md) · [003 — Bot 命令参考](0-foundation/003-command-reference.md) · [004 — 隐私与数据保留](0-foundation/004-privacy-and-retention.md) |
| 1 — 整体架构 | [101 — 系统架构概览](1-architecture/101-system-overview.md) · [102 — 文件目录与交互](1-architecture/102-file-catalog-and-interactions.md) |
| 2 — 实现 | [201 — v0.1 核心下载器](2-implementation/201-v0-1-core-downloader.md) · [202 — v0.2 文件库工作流](2-implementation/202-v0-2-library-workflow.md) |
| 3 — 运维 | [301 — 核心下载器运维](3-operations/301-core-downloader-operations.md) · [302 — 发布安全](3-operations/302-release-security.md) · [303 — v0.2 升级与回滚](3-operations/303-v0-2-upgrade.md) |

项目文档、AI 规则和交接记录统一用英文编写，开发默认读取英文原文；中文译文仅放在 `docs/zh-CN/`，其他位置允许中文语言切换标签。中文保持同名文件和目录结构，修改英文时同步已有译文，内容冲突以英文为准。
编号为稳定标识，不代表执行顺序。编排规则见[英文文档导航](../README.md)。

## 维护约定

根据项目实际需要扩展功能、模块及文档分类，保持职责边界与文档编号稳定。
每次任务完成前必须更新文档：同步受影响的英文文档、已有中文译文及导航，并在本地交接记录中更新结果、验证和下一步。
若共享文档不受影响，在本地交接中说明原因，避免仅为修改而改写。文档同步属于任务完成条件。

## 致谢

TGHarbor 是独立实现的项目，其工作流和使用场景受到现有 Telegram 下载项目的启发，包括 `jsavargas/telethon_downloader`。

## 许可证

Apache License 2.0。
