from __future__ import annotations

from collections.abc import Callable, Sequence
from uuid import UUID

from tgharbor.core.interfaces import JobQueue
from tgharbor.core.jobs import (
    BatchProgress,
    DownloadJob,
    JobStatus,
    NewDownloadJob,
    SourceSession,
)


class UnauthorizedUserError(PermissionError):
    """Raised before an untrusted Telegram user can access the Job API."""


class JobNotFoundError(LookupError):
    """Raised when a job does not exist for its requesting user."""


class JobAPI:
    def __init__(
        self,
        queue: JobQueue,
        authorized_users: tuple[int, ...],
        *,
        max_attempts: int,
        wake_workers: Callable[[], None] | None = None,
    ) -> None:
        self.queue = queue
        self.authorized_users = frozenset(authorized_users)
        self.max_attempts = max_attempts
        self.wake_workers = wake_workers or (lambda: None)

    async def submit(
        self,
        requester_id: int,
        source_session: SourceSession,
        source_chat: str,
        source_message_id: int,
        *,
        destination_name: str | None = None,
        batch_id: str | None = None,
        batch_expected_size: int = 1,
        origin_group_id: str | None = None,
        channel_id: str | None = None,
    ) -> DownloadJob:
        self.require_authorized(requester_id)
        job = await self.queue.enqueue(
            NewDownloadJob(
                requester_id=requester_id,
                source_session=source_session,
                source_chat=source_chat,
                source_message_id=source_message_id,
                destination_name=destination_name,
                max_attempts=self.max_attempts,
                batch_id=batch_id,
                batch_expected_size=batch_expected_size,
                origin_group_id=origin_group_id,
                channel_id=channel_id,
            )
        )
        self.wake_workers()
        return job

    async def get(self, requester_id: int, job_id: UUID) -> DownloadJob:
        self.require_authorized(requester_id)
        job = await self.queue.get_for_requester(job_id, requester_id)
        if job is None:
            raise JobNotFoundError(str(job_id))
        return job

    async def latest(
        self, requester_id: int, *, limit: int = 10, offset: int = 0
    ) -> list[DownloadJob]:
        self.require_authorized(requester_id)
        return await self.queue.latest_for_requester(requester_id, limit=limit, offset=offset)

    async def active(
        self, requester_id: int, *, limit: int = 10, offset: int = 0
    ) -> list[DownloadJob]:
        self.require_authorized(requester_id)
        return await self.queue.latest_for_requester(
            requester_id,
            limit=limit,
            offset=offset,
            statuses=frozenset({JobStatus.PENDING, JobStatus.RUNNING}),
        )

    async def paused(
        self, requester_id: int, *, limit: int = 10, offset: int = 0
    ) -> list[DownloadJob]:
        self.require_authorized(requester_id)
        return await self.queue.list_paused_for_requester(
            requester_id,
            limit=limit,
            offset=offset,
        )

    async def paused_count(self, requester_id: int) -> int:
        self.require_authorized(requester_id)
        return await self.queue.count_paused_for_requester(requester_id)

    async def continue_paused(
        self,
        requester_id: int,
        job_ids: Sequence[UUID] | None = None,
    ) -> int:
        self.require_authorized(requester_id)
        changed = await self.queue.continue_paused_for_requester(requester_id, job_ids)
        if changed:
            self.wake_workers()
        return changed

    async def batch_progress(self, requester_id: int, batch_id: str) -> BatchProgress:
        self.require_authorized(requester_id)
        progress = await self.queue.batch_progress_for_requester(requester_id, batch_id)
        if progress is None:
            raise JobNotFoundError(batch_id)
        return progress

    async def history(
        self,
        requester_id: int,
        *,
        limit: int = 10,
        offset: int = 0,
        statuses: frozenset[JobStatus] | None = None,
    ) -> list[DownloadJob]:
        self.require_authorized(requester_id)
        terminal = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED})
        selected = terminal if statuses is None else statuses & terminal
        return await self.queue.latest_for_requester(
            requester_id,
            limit=limit,
            offset=offset,
            statuses=selected,
        )

    async def resolve(self, requester_id: int, identifier: str) -> DownloadJob:
        self.require_authorized(requester_id)
        job = await self.queue.resolve_for_requester(requester_id, identifier)
        if job is None:
            raise JobNotFoundError(identifier)
        return job

    async def cancel(self, requester_id: int, job_id: UUID) -> DownloadJob:
        self.require_authorized(requester_id)
        try:
            return await self.queue.request_cancel(job_id, requester_id)
        except KeyError as error:
            raise JobNotFoundError(str(job_id)) from error

    def require_authorized(self, requester_id: int) -> None:
        if requester_id not in self.authorized_users:
            raise UnauthorizedUserError("this Telegram user is not authorized")
