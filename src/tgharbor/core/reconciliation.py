from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from tgharbor.core.jobs import DownloadJob, StoredFile


@dataclass(frozen=True, slots=True)
class LegacyMove:
    source_path: Path
    relative_path: Path
    output_path: Path
    size_bytes: int
    state_path: Path


class LegacyRepository(Protocol):
    async def list_unreconciled_completed(
        self, *, limit: int, offset: int
    ) -> list[DownloadJob]: ...

    async def reserve_legacy_name(self, job_id: UUID, desired_name: str) -> DownloadJob: ...

    async def register_legacy_file(
        self,
        job_id: UUID,
        relative_path: Path,
        output_path: Path,
        size_bytes: int,
    ) -> StoredFile: ...


class LegacyStorage(Protocol):
    def normalize_name(self, value: str) -> str: ...

    async def available_legacy_name(self, desired_name: str, job_id: str) -> str: ...

    async def move_legacy_output(self, job: DownloadJob) -> LegacyMove: ...

    async def finish_legacy_move(self, move: LegacyMove) -> None: ...


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    discovered: int
    reconciled: int
    failed: int


class LegacyReconciler:
    def __init__(self, repository: LegacyRepository, storage: LegacyStorage) -> None:
        self.repository = repository
        self.storage = storage

    async def run(self) -> ReconciliationResult:
        jobs = await self._snapshot()
        reconciled = 0
        failed = 0
        for job in jobs:
            try:
                await self._reconcile(job)
            except KeyError, OSError, RuntimeError, ValueError:
                failed += 1
            else:
                reconciled += 1
        return ReconciliationResult(discovered=len(jobs), reconciled=reconciled, failed=failed)

    async def _snapshot(self) -> list[DownloadJob]:
        jobs: list[DownloadJob] = []
        offset = 0
        while True:
            batch = await self.repository.list_unreconciled_completed(limit=100, offset=offset)
            jobs.extend(batch)
            if len(batch) < 100:
                return jobs
            offset += len(batch)

    async def _reconcile(self, job: DownloadJob) -> None:
        if job.output_path is None:
            raise ValueError("completed legacy job has no output path")
        desired = self.storage.normalize_name(job.output_path.name)
        available = await self.storage.available_legacy_name(desired, job.id.hex)
        reserved = await self.repository.reserve_legacy_name(job.id, available)
        move = await self.storage.move_legacy_output(reserved)
        await self.repository.register_legacy_file(
            reserved.id,
            move.relative_path,
            move.output_path,
            move.size_bytes,
        )
        await self.storage.finish_legacy_move(move)
