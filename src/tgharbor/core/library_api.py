from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from tgharbor.core.job_api import UnauthorizedUserError
from tgharbor.core.jobs import LibraryStats, StoredFile


@dataclass(frozen=True, slots=True)
class PendingOrganization:
    job_id: UUID
    operator_id: int
    action: str
    relative_path: Path
    output_path: Path


class LibraryRepository(Protocol):
    async def list_files(self, *, limit: int, offset: int) -> list[StoredFile]: ...

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
        return await self.storage.list_archive_directories()

    async def stats(self, actor_id: int) -> LibraryStats:
        self.require_authorized(actor_id)
        return await self.repository.library_stats()

    async def resolve(self, actor_id: int, identifier: str) -> StoredFile:
        self.require_authorized(actor_id)
        stored = await self.repository.resolve_file(identifier)
        if stored is None:
            raise StoredFileNotFoundError(identifier)
        return stored

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
        await self.storage.finish_organization(stored.job_id)
        return updated

    async def mkdir(self, actor_id: int, directory: str) -> Path:
        self.require_authorized(actor_id)
        relative = await self.storage.create_archive_directory(directory)
        await self.repository.record_directory_creation(actor_id, relative)
        return relative


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
