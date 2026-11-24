from pathlib import Path
from uuid import UUID

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


async def test_job_api_carries_batch_metadata_and_scopes_progress(tmp_path: Path) -> None:
    queue = SQLiteJobQueue(tmp_path / "jobs.db")
    await queue.initialize()
    api = JobAPI(queue, (1001, 1002), max_attempts=4)
    first = await api.submit(
        1001,
        SourceSession.BOT,
        "1001",
        1,
        batch_id="opaque-batch",
        batch_expected_size=2,
        origin_group_id="-100123",
        channel_id="-100456",
    )
    await api.submit(
        1001,
        SourceSession.BOT,
        "1001",
        2,
        batch_id="opaque-batch",
        batch_expected_size=2,
    )

    assert first.batch_id == "opaque-batch"
    assert first.batch_expected_size == 2
    assert first.origin_group_id == "-100123"
    assert first.channel_id == "-100456"

    progress = await api.batch_progress(1001, "opaque-batch")
    assert progress.expected_size == 2
    assert progress.total_jobs == 2
    assert progress.pending == 2
    assert not progress.all_succeeded
    with pytest.raises(JobNotFoundError):
        await api.batch_progress(1002, "opaque-batch")
    with pytest.raises(UnauthorizedUserError):
        await api.batch_progress(9999, "opaque-batch")


async def test_paused_jobs_are_requester_scoped_and_continuation_is_idempotent(
    tmp_path: Path,
) -> None:
    queue = SQLiteJobQueue(tmp_path / "jobs.db")
    await queue.initialize()
    wakes: list[None] = []
    api = JobAPI(
        queue,
        (1001, 1002),
        max_attempts=2,
        wake_workers=lambda: wakes.append(None),
    )
    job = await api.submit(1001, SourceSession.BOT, "1001", 3)
    running = await queue.claim_next("worker")
    assert running is not None and running.id == job.id
    assert await queue.recover_interrupted() == (1, 0)

    assert await api.paused(1002) == []
    assert await api.paused_count(1002) == 0
    with pytest.raises(UnauthorizedUserError):
        await api.paused(9999)
    paused = await api.paused(1001)
    assert len(paused) == 1 and paused[0].id == job.id
    assert paused[0].resume_required
    assert await api.paused_count(1001) == 1

    wakes_before = len(wakes)
    assert await api.continue_paused(1002, [job.id]) == 0
    assert await api.continue_paused(1001, []) == 0
    assert await api.continue_paused(1001, [UUID(int=1)]) == 0
    assert await api.continue_paused(1001, [job.id]) == 1
    assert len(wakes) == wakes_before + 1
    assert await api.continue_paused(1001, [job.id]) == 0
    assert len(wakes) == wakes_before + 1
    assert await api.paused(1001) == []

    resumed = await queue.claim_next("worker")
    assert resumed is not None and resumed.id == job.id
    assert resumed.attempt_count == 2
