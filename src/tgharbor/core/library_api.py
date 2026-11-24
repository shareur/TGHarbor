from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID

from tgharbor.core.job_api import UnauthorizedUserError
from tgharbor.core.jobs import BatchProgress, FileLocation, LibraryStats, StoredFile

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PendingOrganization:
    job_id: UUID
    operator_id: int
    action: str
    relative_path: Path
    output_path: Path


class LibraryRepository(Protocol):
    async def list_files(self, *, limit: int, offset: int) -> list[StoredFile]: ...

    async def list_files_for_batch(self, requester_id: int, batch_id: str) -> list[StoredFile]: ...

    async def batch_progress_for_requester(
        self, requester_id: int, batch_id: str
    ) -> BatchProgress | None: ...

    async def archive_directory_recency(self) -> dict[str, float]: ...

    async def resolve_file(self, identifier: str) -> StoredFile | None: ...

    async def library_stats(self) -> LibraryStats: ...

    async def record_file_move(
        self,
        job_id: UUID,
        operator_id: int,
        relative_path: Path,
        output_path: Path,
        *,
        action: str,
    ) -> StoredFile: ...

    async def record_directory_creation(self, operator_id: int, relative_path: Path) -> None: ...


class LibraryStorage(Protocol):
    root: Path

    async def organize(
        self,
        source: Path,
        job_id: str,
        *,
        new_name: str | None = None,
        archive_directory: str | None = None,
        operator_id: int | None = None,
        action: str | None = None,
    ) -> Path: ...

    async def create_archive_directory(self, value: str) -> Path: ...

    async def list_archive_directories(self) -> list[Path]: ...

    async def pending_organizations(self) -> list[PendingOrganization]: ...

    async def finish_organization(self, job_id: UUID) -> None: ...


class StoredFileNotFoundError(LookupError):
    """Raised when a shared stored-file identifier is absent or ambiguous."""


class BatchNotFoundError(LookupError):
    """Raised when a batch is absent inside the requester's authorization scope."""


class BatchMoveCategory(StrEnum):
    MOVED = "moved"
    ALREADY_MOVED = "already-moved"
    MISSING = "missing"
    NOT_READY = "not-ready"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class BatchMoveItem:
    job_id: UUID
    category: BatchMoveCategory


@dataclass(frozen=True, slots=True)
class BatchMoveResult:
    batch_id: str
    total_members: int
    moved: int
    already_moved: int
    missing: int
    not_ready: int
    failed: int
    items: tuple[BatchMoveItem, ...]

    @property
    def complete(self) -> bool:
        return self.missing == 0 and self.not_ready == 0 and self.failed == 0


class LibraryAPI:
    def __init__(
        self,
        repository: LibraryRepository,
        storage: LibraryStorage,
        authorized_users: tuple[int, ...],
    ) -> None:
        self.repository = repository
        self.storage = storage
        self.authorized_users = frozenset(authorized_users)

    def require_authorized(self, actor_id: int) -> None:
        if actor_id not in self.authorized_users:
            raise UnauthorizedUserError("this Telegram user is not authorized")

    async def files(self, actor_id: int, *, limit: int = 10, offset: int = 0) -> list[StoredFile]:
        self.require_authorized(actor_id)
        return await self.repository.list_files(limit=limit, offset=offset)

    async def directories(self, actor_id: int) -> list[Path]:
        self.require_authorized(actor_id)
        existing = await self.storage.list_archive_directories()
        recency = await self.repository.archive_directory_recency()
        return order_archive_directories(existing, recency)

    async def stats(self, actor_id: int) -> LibraryStats:
        self.require_authorized(actor_id)
        return await self.repository.library_stats()

    async def resolve(self, actor_id: int, identifier: str) -> StoredFile:
        self.require_authorized(actor_id)
        stored = await self.repository.resolve_file(identifier)
        if stored is None:
            raise StoredFileNotFoundError(identifier)
        return stored

    async def files_for_batch(self, actor_id: int, batch_id: str) -> list[StoredFile]:
        self.require_authorized(actor_id)
        return await self.repository.list_files_for_batch(actor_id, batch_id)

    async def organize(
        self,
        actor_id: int,
        identifier: str,
        *,
        new_name: str | None = None,
        archive_directory: str | None = None,
    ) -> StoredFile:
        stored = await self.resolve(actor_id, identifier)
        if new_name and archive_directory is not None:
            action = "rename-and-archive"
        elif archive_directory is not None:
            action = "archive"
        else:
            action = "rename"
        relative = await self.storage.organize(
            stored.relative_path,
            stored.job_id.hex,
            new_name=new_name,
            archive_directory=archive_directory,
            operator_id=actor_id,
            action=action,
        )
        updated = await self.repository.record_file_move(
            stored.job_id,
            actor_id,
            relative,
            self.storage.root / relative,
            action=action,
        )
        # The catalog move above is the committed effect. Removing the on-disk
        # organization marker is cleanup: if it fails or is interrupted, startup
        # reconciliation replays the same idempotent move without a duplicate
        # audit row. Surfacing that cleanup failure as an organize failure would
        # make the caller believe nothing committed and restore a duplicate
        # confirmation.
        try:
            await self.storage.finish_organization(stored.job_id)
        except Exception as error:  # noqa: BLE001 - reconciliation owns cleanup replay
            logger.warning(
                "Organization cleanup deferred: outcome=deferred error_type=%s",
                type(error).__name__,
            )
        return updated

    async def organize_batch(
        self,
        actor_id: int,
        batch_id: str,
        target_directory: str,
    ) -> BatchMoveResult:
        """Move current Inbox members of one requester-owned batch.

        Processing is deliberately per-file and idempotent. Already organized
        or non-Inbox members are skipped; one failure does not roll back another
        file. The result contains stable categories and identifiers, never
        absolute filesystem paths.
        """

        self.require_authorized(actor_id)
        progress = await self.repository.batch_progress_for_requester(actor_id, batch_id)
        if progress is None:
            raise BatchNotFoundError(batch_id)
        files = await self.repository.list_files_for_batch(actor_id, batch_id)
        items: list[BatchMoveItem] = []
        moved = 0
        already_moved = 0
        missing = 0
        failed = 0
        for stored in files:
            if stored.location is FileLocation.ARCHIVE:
                already_moved += 1
                items.append(BatchMoveItem(stored.job_id, BatchMoveCategory.ALREADY_MOVED))
                continue
            try:
                updated = await self.organize(
                    actor_id,
                    stored.job_id.hex,
                    archive_directory=target_directory,
                )
            except StoredFileNotFoundError:
                missing += 1
                items.append(BatchMoveItem(stored.job_id, BatchMoveCategory.MISSING))
            except KeyError, OSError, RuntimeError, ValueError:
                failed += 1
                items.append(BatchMoveItem(stored.job_id, BatchMoveCategory.FAILED))
            else:
                if updated.location is FileLocation.ARCHIVE:
                    moved += 1
                    items.append(BatchMoveItem(stored.job_id, BatchMoveCategory.MOVED))
                else:
                    failed += 1
                    items.append(BatchMoveItem(stored.job_id, BatchMoveCategory.FAILED))
        missing += max(0, progress.completed - len(files))
        not_ready = max(0, progress.total_jobs - progress.completed)
        return BatchMoveResult(
            batch_id=batch_id,
            total_members=progress.total_jobs,
            moved=moved,
            already_moved=already_moved,
            missing=missing,
            not_ready=not_ready,
            failed=failed,
            items=tuple(items),
        )

    async def mkdir(self, actor_id: int, directory: str) -> Path:
        self.require_authorized(actor_id)
        relative = await self.storage.create_archive_directory(directory)
        await self.repository.record_directory_creation(actor_id, relative)
        return relative


def order_archive_directories(directories: list[Path], recency: dict[str, float]) -> list[Path]:
    """Order existing directories by latest use, then case-insensitive name."""

    def sort_key(directory: Path) -> tuple[bool, float, str, str]:
        value = recency.get(directory.as_posix())
        if value is None:
            return (True, 0.0, directory.as_posix().casefold(), directory.as_posix())
        return (False, -value, directory.as_posix().casefold(), directory.as_posix())

    return sorted(directories, key=sort_key)


@dataclass(frozen=True, slots=True)
class OrganizationRecoveryResult:
    discovered: int
    recovered: int
    failed: int


async def recover_pending_organizations(
    repository: LibraryRepository, storage: LibraryStorage
) -> OrganizationRecoveryResult:
    pending = await storage.pending_organizations()
    recovered = 0
    failed = 0
    for move in pending:
        try:
            await repository.record_file_move(
                move.job_id,
                move.operator_id,
                move.relative_path,
                move.output_path,
                action=move.action,
            )
            await storage.finish_organization(move.job_id)
        except KeyError, OSError, RuntimeError, ValueError:
            failed += 1
        else:
            recovered += 1
    return OrganizationRecoveryResult(len(pending), recovered, failed)
