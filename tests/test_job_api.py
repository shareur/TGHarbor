from pathlib import Path

import pytest

from tgharbor.core.job_api import JobAPI, JobNotFoundError, UnauthorizedUserError
from tgharbor.core.jobs import SourceSession
from tgharbor.persistence import SQLiteJobQueue


async def test_job_api_authorizes_before_queue_access(tmp_path: Path) -> None:
    queue = SQLiteJobQueue(tmp_path / "jobs.db")
    await queue.initialize()
    api = JobAPI(queue, (1001,), max_attempts=3)

    with pytest.raises(UnauthorizedUserError):
        await api.submit(9999, SourceSession.BOT, "9999", 1)

    assert await queue.claim_next("worker") is None


async def test_job_api_scopes_queries_and_cancel_to_owner(tmp_path: Path) -> None:
    queue = SQLiteJobQueue(tmp_path / "jobs.db")
    await queue.initialize()
    api = JobAPI(queue, (1001, 1002), max_attempts=4)
    job = await api.submit(1001, SourceSession.USER, "channel", 2)

    with pytest.raises(JobNotFoundError):
        await api.get(1002, job.id)
    with pytest.raises(JobNotFoundError):
        await api.cancel(1002, job.id)

    cancelled = await api.cancel(1001, job.id)
    assert cancelled.status.value == "cancelled"
