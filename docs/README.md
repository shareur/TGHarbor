# Documentation

[Project overview](../README.md) · [简体中文](zh-CN/README.md)

| Category | Documents |
| --- | --- |
| 0 — Foundation | [001 — Development setup](0-foundation/001-development-setup.md) · [002 — Feature overview](0-foundation/002-feature-overview.md) · [003 — Bot command reference](0-foundation/003-command-reference.md) · [004 — Privacy and data retention](0-foundation/004-privacy-and-retention.md) |
| 1 — Architecture | [101 — System overview](1-architecture/101-system-overview.md) · [102 — File catalog and interactions](1-architecture/102-file-catalog-and-interactions.md) |
| 2 — Implementation | [201 — v0.1 Core Downloader](2-implementation/201-v0-1-core-downloader.md) · [202 — v0.2 Library workflow](2-implementation/202-v0-2-library-workflow.md) |
| 3 — Operations | [301 — Core Downloader operations](3-operations/301-core-downloader-operations.md) · [302 — Release security](3-operations/302-release-security.md) · [303 — v0.2 upgrade and rollback](3-operations/303-v0-2-upgrade.md) |

## Organization

- `0-foundation/` (`001`–`099`): scope, terminology, development setup, contribution guidelines.
- `1-architecture/` (`101`–`199`): system design, module boundaries, data models, interfaces.
- `2-implementation/` (`201`–`299`): implementation plans, development sequence, testing and acceptance.
- `3-operations/` (`301`–`399`): deployment, configuration, monitoring, backups, troubleshooting.

Use `Nxx-topic-name.md`, where `N` is the category and `xx` starts at `01`.
Published numbers remain stable and are never reassigned; append new documents using
the next available number. Numbers identify documents, not execution order.
Keep one topic per document and link to related material instead of duplicating it.
Create directories only when adding actual documents. Update navigation and affected
links whenever documents are added, moved, or removed.

Write all project documentation, AI instructions, and handoffs in English. English
is authoritative for development; read English sources by default. Chinese
translations belong only in `zh-CN/` (Chinese language-switch labels are allowed
elsewhere). Chinese translations mirror filenames and subdirectories
under `zh-CN/` and are updated alongside their English counterparts, with reciprocal
language links. The root project README maps to `zh-CN/README.md`, which also serves
as the Chinese documentation index. Index files are not numbered.

## Maintenance

Extend features, modules, and documentation categories as project needs arise,
while preserving established boundaries and stable document identifiers.
Every completed task must include documentation updates: revise affected English
documents, existing Chinese translations, and navigation, and refresh the local
handoff with results, verification, and next steps. If shared documentation is
unaffected, record the reason in the local handoff instead of making cosmetic edits.
Documentation synchronization is part of task completion.
