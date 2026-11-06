from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from tgharbor.core.jobs import DownloadJob, NewDownloadJob

ProgressCallback = Callable[[int, int | None], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class MediaDescriptor:
    file_name: str
    total_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class StorageTarget:
    temporary_path: Path
    final_path: Path


class JobQueue(Protocol):
    async def enqueue(self, job: NewDownloadJob) -> DownloadJob: ...

    async def get(self, job_id: UUID) -> DownloadJob | None: ...

    async def get_for_requester(self, job_id: UUID, requester_id: int) -> DownloadJob | None: ...

    async def latest_for_requester(
        self, requester_id: int, *, limit: int = 10
    ) -> list[DownloadJob]: ...

    async def claim_next(self, worker_id: str) -> DownloadJob | None: ...

    async def update_progress(
        self, job_id: UUID, worker_id: str, current: int, total: int | None
    ) -> DownloadJob: ...

    async def complete(self, job_id: UUID, worker_id: str, output_path: Path) -> DownloadJob: ...

    async def fail_or_retry(
        self,
        job_id: UUID,
        worker_id: str,
        error: str,
        *,
        retryable: bool,
        retry_delay_seconds: float = 0,
    ) -> DownloadJob: ...

    async def request_cancel(self, job_id: UUID, requester_id: int) -> DownloadJob: ...

    async def is_cancel_requested(self, job_id: UUID, worker_id: str) -> bool: ...

    async def mark_cancelled(self, job_id: UUID, worker_id: str) -> DownloadJob: ...


class DownloadProvider(Protocol):
    async def describe(self, job: DownloadJob) -> MediaDescriptor: ...

    async def download(
        self, job: DownloadJob, destination: Path, progress: ProgressCallback
    ) -> None: ...


class Storage(Protocol):
    async def prepare(self, job: DownloadJob, suggested_name: str) -> StorageTarget: ...

    async def commit(self, target: StorageTarget) -> Path: ...

    async def discard(self, target: StorageTarget) -> None: ...

    def is_complete(self, target: StorageTarget) -> bool: ...


class JobEventSink(Protocol):
    async def job_finished(self, job: DownloadJob) -> None: ...
