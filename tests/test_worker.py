from __future__ import annotations

import asyncio
from pathlib import Path

from tgharbor.core.errors import PermanentDownloadError, RetryableDownloadError
from tgharbor.core.interfaces import MediaDescriptor, ProgressCallback
from tgharbor.core.jobs import DownloadJob, JobStatus, NewDownloadJob, SourceSession
from tgharbor.core.worker import DownloadWorker, WorkerSettings
from tgharbor.persistence import SQLiteJobQueue
from tgharbor.storage import LocalStorage


class SuccessfulProvider:
    def __init__(self, content: bytes = b"payload") -> None:
        self.content = content
        self.download_count = 0

    async def describe(self, job: DownloadJob) -> MediaDescriptor:
        return MediaDescriptor("media.bin", len(self.content))

    async def download(
        self, job: DownloadJob, destination: Path, progress: ProgressCallback
    ) -> None:
        self.download_count += 1
        destination.write_bytes(self.content)
        await progress(len(self.content), len(self.content))


class FailingProvider(SuccessfulProvider):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    async def download(
        self, job: DownloadJob, destination: Path, progress: ProgressCallback
    ) -> None:
        self.download_count += 1
        raise self.error


class BlockingProvider(SuccessfulProvider):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def download(
        self, job: DownloadJob, destination: Path, progress: ProgressCallback
    ) -> None:
        self.download_count += 1
        self.started.set()
        await asyncio.Event().wait()


class FailingNotifier:
    async def job_finished(self, job: DownloadJob) -> None:
        raise OSError("notification unavailable")


async def components(
    tmp_path: Path, provider: SuccessfulProvider, *, timeout: float = 1
) -> tuple[SQLiteJobQueue, LocalStorage, DownloadWorker]:
    queue = SQLiteJobQueue(tmp_path / "state/tgharbor.db")
    await queue.initialize()
    storage = LocalStorage(tmp_path / "downloads")
    await storage.initialize()
    worker = DownloadWorker(
        queue,
        {SourceSession.BOT: provider},
        storage,
        WorkerSettings(
            timeout_seconds=timeout,
            retry_base_seconds=0.001,
            progress_interval_seconds=0,
            cancellation_poll_seconds=0.005,
            idle_poll_seconds=0.01,
        ),
    )
    return queue, storage, worker


async def enqueue(queue: SQLiteJobQueue, message_id: int, *, max_attempts: int = 3) -> DownloadJob:
    return await queue.enqueue(
        NewDownloadJob(
            requester_id=1001,
            source_session=SourceSession.BOT,
            source_chat="1001",
            source_message_id=message_id,
            max_attempts=max_attempts,
        )
    )


async def test_worker_downloads_commits_and_completes(tmp_path: Path) -> None:
    provider = SuccessfulProvider()
    queue, _, worker = await components(tmp_path, provider)
    pending = await enqueue(queue, 1)

    assert await worker.run_once("worker")

    completed = await queue.get(pending.id)
    assert completed is not None and completed.status is JobStatus.COMPLETED
    assert completed.progress_bytes == len(provider.content)
    assert completed.output_path is not None
    assert completed.output_path.read_bytes() == provider.content


async def test_retryable_provider_error_is_requeued_with_provider_delay(tmp_path: Path) -> None:
    provider = FailingProvider(RetryableDownloadError("rate limited", retry_after_seconds=60))
    queue, _, worker = await components(tmp_path, provider)
    pending = await enqueue(queue, 2)

    await worker.run_once("worker")

    retried = await queue.get(pending.id)
    assert retried is not None and retried.status is JobStatus.PENDING
    assert retried.error == "rate limited"
    assert (retried.available_at - retried.updated_at).total_seconds() >= 59


async def test_permanent_provider_error_fails_immediately(tmp_path: Path) -> None:
    provider = FailingProvider(PermanentDownloadError("message has no media"))
    queue, _, worker = await components(tmp_path, provider)
    pending = await enqueue(queue, 3)

    await worker.run_once("worker")

    failed = await queue.get(pending.id)
    assert failed is not None and failed.status is JobStatus.FAILED
    assert failed.attempt_count == 1


async def test_timeout_requeues_job(tmp_path: Path) -> None:
    provider = BlockingProvider()
    queue, _, worker = await components(tmp_path, provider, timeout=0.02)
    pending = await enqueue(queue, 4)

    await worker.run_once("worker")

    retried = await queue.get(pending.id)
    assert retried is not None and retried.status is JobStatus.PENDING
    assert retried.error == "download timed out"


async def test_running_cancel_stops_provider_and_marks_cancelled(tmp_path: Path) -> None:
    provider = BlockingProvider()
    queue, _, worker = await components(tmp_path, provider)
    pending = await enqueue(queue, 5)
    task = asyncio.create_task(worker.run_once("worker"))
    await provider.started.wait()

    await queue.request_cancel(pending.id, 1001)
    await asyncio.wait_for(task, timeout=1)

    cancelled = await queue.get(pending.id)
    assert cancelled is not None and cancelled.status is JobStatus.CANCELLED


async def test_worker_reconciles_completed_file_without_redownload(tmp_path: Path) -> None:
    provider = SuccessfulProvider()
    queue, storage, worker = await components(tmp_path, provider)
    pending = await enqueue(queue, 6)
    claimed = await queue.claim_next("crashed-worker")
    assert claimed is not None
    target = await storage.prepare(claimed, "media.bin")
    target.temporary_path.write_bytes(provider.content)
    await storage.commit(target)
    await queue.recover_interrupted()

    await worker.run_once("replacement-worker")

    completed = await queue.get(pending.id)
    assert completed is not None and completed.status is JobStatus.COMPLETED
    assert provider.download_count == 0


async def test_worker_cancellation_requeues_running_job(tmp_path: Path) -> None:
    provider = BlockingProvider()
    queue, _, worker = await components(tmp_path, provider)
    pending = await enqueue(queue, 7)
    task = asyncio.create_task(worker.run_once("worker"))
    await provider.started.wait()

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    retried = await queue.get(pending.id)
    assert retried is not None and retried.status is JobStatus.PENDING
    assert retried.error == "worker stopped"


async def test_notification_failure_does_not_change_completed_result(tmp_path: Path) -> None:
    provider = SuccessfulProvider()
    queue, storage, _ = await components(tmp_path, provider)
    worker = DownloadWorker(
        queue,
        {SourceSession.BOT: provider},
        storage,
        WorkerSettings(timeout_seconds=1, retry_base_seconds=1),
        FailingNotifier(),
    )
    pending = await enqueue(queue, 8)

    await worker.run_once("worker")

    completed = await queue.get(pending.id)
    assert completed is not None and completed.status is JobStatus.COMPLETED
