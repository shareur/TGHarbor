from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tgharbor.core.jobs import JobStatus, NewDownloadJob, SourceSession
from tgharbor.persistence import InvalidJobTransition, SQLiteJobQueue


def new_job(message_id: int, *, max_attempts: int = 3) -> NewDownloadJob:
    return NewDownloadJob(
        requester_id=1001,
        source_session=SourceSession.BOT,
        source_chat="1001",
        source_message_id=message_id,
        max_attempts=max_attempts,
    )


async def initialized_queue(tmp_path: Path) -> SQLiteJobQueue:
    queue = SQLiteJobQueue(tmp_path / "state/tgharbor.db")
    await queue.initialize()
    return queue


async def test_enqueue_is_deduplicated_and_scoped_to_requester(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    request = new_job(10)

    first = await queue.enqueue(request)
    duplicate = await queue.enqueue(request)

    assert first.id == duplicate.id
    assert first.status is JobStatus.PENDING
    assert await queue.get_for_requester(first.id, 1001) == first
    assert await queue.get_for_requester(first.id, 9999) is None


async def test_concurrent_claims_use_independent_connections_without_duplicates(
    tmp_path: Path,
) -> None:
    queue = await initialized_queue(tmp_path)
    jobs = [await queue.enqueue(new_job(index)) for index in range(1, 9)]

    claimed = await asyncio.gather(
        *(queue.claim_next(f"worker-{index}") for index in range(len(jobs)))
    )

    claimed_jobs = [job for job in claimed if job is not None]
    assert len(claimed_jobs) == len(jobs)
    assert len({job.id for job in claimed_jobs}) == len(jobs)
    assert all(job.status is JobStatus.RUNNING for job in claimed_jobs)
    assert all(job.attempt_count == 1 for job in claimed_jobs)


async def test_retry_is_delayed_and_exhaustion_is_terminal(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    pending = await queue.enqueue(new_job(20, max_attempts=2))
    first = await queue.claim_next("worker", now=pending.created_at.timestamp())
    assert first is not None

    retry = await queue.fail_or_retry(
        first.id,
        "worker",
        "temporary outage",
        retryable=True,
        retry_delay_seconds=10,
    )

    assert retry.status is JobStatus.PENDING
    assert await queue.claim_next("worker", now=retry.available_at.timestamp() - 1) is None
    second = await queue.claim_next("worker", now=retry.available_at.timestamp() + 0.001)
    assert second is not None
    failed = await queue.fail_or_retry(
        second.id,
        "worker",
        "temporary outage",
        retryable=True,
    )
    assert failed.status is JobStatus.FAILED
    assert failed.attempt_count == 2
    assert await queue.claim_next("worker") is None


async def test_only_claim_owner_can_update_and_complete(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    await queue.enqueue(new_job(30))
    claimed = await queue.claim_next("owner")
    assert claimed is not None

    with pytest.raises(InvalidJobTransition):
        await queue.update_progress(claimed.id, "other", 1, 10)

    progressed = await queue.update_progress(claimed.id, "owner", 5, 10)
    completed = await queue.complete(claimed.id, "owner", Path("/downloads/file.bin"))

    assert progressed.progress_bytes == 5
    assert completed.status is JobStatus.COMPLETED
    assert completed.output_path == Path("/downloads/file.bin")
    with pytest.raises(InvalidJobTransition):
        await queue.complete(claimed.id, "owner", Path("/downloads/again.bin"))


async def test_cancel_pending_and_request_cancel_running(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    pending = await queue.enqueue(new_job(40))
    cancelled = await queue.request_cancel(pending.id, 1001)
    assert cancelled.status is JobStatus.CANCELLED

    await queue.enqueue(new_job(41))
    running = await queue.claim_next("worker")
    assert running is not None
    requested = await queue.request_cancel(running.id, 1001)
    assert requested.status is JobStatus.RUNNING
    assert requested.cancel_requested
    assert await queue.is_cancel_requested(running.id, "worker")
    final = await queue.mark_cancelled(running.id, "worker")
    assert final.status is JobStatus.CANCELLED


async def test_restart_recovery_requeues_or_exhausts_running_jobs(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    await queue.enqueue(new_job(50, max_attempts=2))
    recoverable = await queue.claim_next("worker-a")
    assert recoverable is not None

    await queue.enqueue(new_job(51, max_attempts=1))
    exhausted = await queue.claim_next("worker-b")
    assert exhausted is not None

    recovered_count, exhausted_count = await queue.recover_interrupted(now=1000)

    assert (recovered_count, exhausted_count) == (1, 1)
    recovered = await queue.get(recoverable.id)
    failed = await queue.get(exhausted.id)
    assert recovered is not None and recovered.status is JobStatus.PENDING
    assert recovered.claimed_by is None
    assert failed is not None and failed.status is JobStatus.FAILED


async def test_restart_recovery_preserves_a_running_cancellation(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    await queue.enqueue(new_job(52))
    running = await queue.claim_next("worker")
    assert running is not None
    await queue.request_cancel(running.id, 1001)

    recovered_count, exhausted_count = await queue.recover_interrupted(now=1000)

    cancelled = await queue.get(running.id)
    assert (recovered_count, exhausted_count) == (0, 0)
    assert cancelled is not None and cancelled.status is JobStatus.CANCELLED


async def test_schema_newer_than_application_is_rejected(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    import aiosqlite

    async with aiosqlite.connect(queue.path) as connection:
        await connection.execute("PRAGMA user_version = 999")
        await connection.commit()

    with pytest.raises(RuntimeError, match="newer than supported"):
        await queue.initialize()
