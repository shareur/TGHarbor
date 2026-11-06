from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from tgharbor.core.interfaces import JobQueue
from tgharbor.core.jobs import DownloadJob, NewDownloadJob, SourceSession


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

    async def latest(self, requester_id: int, *, limit: int = 10) -> list[DownloadJob]:
        self.require_authorized(requester_id)
        return await self.queue.latest_for_requester(requester_id, limit=limit)

    async def cancel(self, requester_id: int, job_id: UUID) -> DownloadJob:
        self.require_authorized(requester_id)
        try:
            return await self.queue.request_cancel(job_id, requester_id)
        except KeyError as error:
            raise JobNotFoundError(str(job_id)) from error

    def require_authorized(self, requester_id: int) -> None:
        if requester_id not in self.authorized_users:
            raise UnauthorizedUserError("this Telegram user is not authorized")
