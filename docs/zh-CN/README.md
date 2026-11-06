# TGHarbor

[English](../../README.md)

**可自托管的 Telegram 下载与归档管理工具。**

TGHarbor 采用净室方式独立实现，用于下载、整理和归档 Telegram 内容。

> 项目处于早期开发阶段，v1.0 之前配置和内部 API 可能发生变化。

## v0.1 目标

- Telegram 身份验证
- 接收授权用户提交的媒体
- 基于 SQLite 的持久下载队列
- 异步下载 Worker
- 本地存储
- 进度报告
- 重试与重启恢复
- 以 Docker 为主要部署方式

## 开发

环境准备、检查、启动及配置方式见[开发准备](0-foundation/001-development-setup.md)。

## 当前状态

**v0.1 — 核心下载器**实现已可供本地验收。自动化测试在不使用真实凭据的情况下覆盖队列、恢复、存储、Worker、授权、Telegram 适配器和生命周期。部署验收仍需真实 Telegram 账号、Bot Token 与 Docker 主机。

## 文档导航

| 分类 | 文档 |
| --- | --- |
| 0 — 准备 | [001 — 开发准备](0-foundation/001-development-setup.md) |
| 1 — 整体架构 | [101 — 系统架构概览](1-architecture/101-system-overview.md) |
| 2 — 实现 | [201 — v0.1 核心下载器](2-implementation/201-v0-1-core-downloader.md) |
| 3 — 运维 | [301 — 核心下载器运维](3-operations/301-core-downloader-operations.md) |

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
