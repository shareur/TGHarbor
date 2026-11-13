# Documentation

[Project overview](../README.md) · [简体中文](zh-CN/README.md)

| Category | Documents |
| --- | --- |
| 0 — Foundation | [001 — Development setup](0-foundation/001-development-setup.md) · [002 — Feature overview](0-foundation/002-feature-overview.md) · [003 — Bot command reference](0-foundation/003-command-reference.md) · [004 — Privacy and data retention](0-foundation/004-privacy-and-retention.md) |
| 1 — Architecture | [101 — System overview](1-architecture/101-system-overview.md) · [102 — File catalog and interactions](1-architecture/102-file-catalog-and-interactions.md) |
| 2 — Implementation | [201 — v0.1 Core Downloader](2-implementation/201-v0-1-core-downloader.md) · [202 — v0.2 Library workflow](2-implementation/202-v0-2-library-workflow.md) |
| 3 — Operations | [301 — Core Downloader operations](3-operations/301-core-downloader-operations.md) · [302 — Release security](3-operations/302-release-security.md) · [303 — v0.2 upgrade and rollback](3-operations/303-v0-2-upgrade.md) |

## Organization

- `0-foundation/` (`001`–`099`): product scope, terminology, development setup, command
  reference, privacy, and contribution guidance.
- `1-architecture/` (`101`–`199`): public component concepts, data categories,
  authorization boundaries, and observable operational guarantees.
- `2-implementation/` (`201`–`299`): release scope, exclusions, acceptance criteria, and
  verification evidence.
- `3-operations/` (`301`–`399`): deployment, configuration, monitoring, backup and
  restore, upgrade and rollback, release verification, and troubleshooting.

Use `Nxx-topic-name.md`, where `N` is the category and `xx` starts at `01`.
Published numbers remain stable and are never reassigned; append new documents using
the next available number. Numbers identify documents, not execution order.
Keep one topic per document and link to related material instead of duplicating it.
Create directories only when adding actual documents. Update navigation and affected
links whenever documents are added, moved, or removed.

## Language and maintenance

English is authoritative. Chinese translations mirror filenames and subdirectories
under `zh-CN/` and are updated alongside their English counterparts, with reciprocal
language links. The root project README maps to `zh-CN/README.md`, which also serves as
the Chinese documentation index. Index files are not numbered.

Extend categories only when adding actual documents. Avoid empty placeholders and
duplicate topics, and keep examples and command descriptions consistent with observable
behavior.
