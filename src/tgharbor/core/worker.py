from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass

from tgharbor.core.errors import (
    DownloadError,
    JobCancellationRequested,
)
from tgharbor.core.interfaces import (
    DownloadProvider,
    JobEventSink,
    JobQueue,
    Storage,
    StorageTarget,
)
from tgharbor.core.jobs import DownloadJob, SourceSession

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    timeout_seconds: float
    retry_base_seconds: float
    progress_interval_seconds: float = 1.0
    cancellation_poll_seconds: float = 0.25
    idle_poll_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.retry_base_seconds <= 0:
            raise ValueError("retry_base_seconds must be positive")
        if self.progress_interval_seconds < 0:
            raise ValueError("progress_interval_seconds must not be negative")
        if self.cancellation_poll_seconds <= 0 or self.idle_poll_seconds <= 0:
            raise ValueError("poll intervals must be positive")


class DownloadWorker:
    def __init__(
        self,
        queue: JobQueue,
        providers: Mapping[SourceSession, DownloadProvider],
        storage: Storage,
        settings: WorkerSettings,
        event_sink: JobEventSink | None = None,
    ) -> None:
        self.queue = queue
        self.providers = providers
        self.storage = storage
        self.settings = settings
        self.event_sink = event_sink

    async def run_once(self, worker_id: str) -> bool:
        job = await self.queue.claim_next(worker_id)
        if job is None:
            return False
        await self._process(job, worker_id)
        return True

    async def _process(self, job: DownloadJob, worker_id: str) -> None:
        target: StorageTarget | None = None
        try:
            provider = self.providers[job.source_session]
            async with asyncio.timeout(self.settings.timeout_seconds):
                descriptor = await provider.describe(job)
                target = await self.storage.prepare(
                    job, job.destination_name or descriptor.file_name
                )
                if self.storage.is_complete(target):
                    completed = await self.queue.complete(job.id, worker_id, target.final_path)
                    await self._publish(completed)
                    return
                progress = _ProgressReporter(
                    self.queue,
                    job,
                    worker_id,
                    interval_seconds=self.settings.progress_interval_seconds,
                )
                await self._download_with_cancellation(provider, job, target, progress, worker_id)
                if await self.queue.is_cancel_requested(job.id, worker_id):
                    raise JobCancellationRequested
                output = await self.storage.commit(target)
                completed = await self.queue.complete(job.id, worker_id, output)
                await self._publish(completed)
        except JobCancellationRequested:
            if target is not None:
                await self.storage.discard(target)
            cancelled = await self.queue.mark_cancelled(job.id, worker_id)
            await self._publish(cancelled)
        except TimeoutError:
            if target is not None:
                await self.storage.discard(target)
            await self._retry(job, worker_id, "download timed out", retryable=True)
        except DownloadError as error:
            if target is not None:
                await self.storage.discard(target)
            await self._retry(
                job,
                worker_id,
                _safe_error(error),
                retryable=error.retryable,
                retry_after_seconds=error.retry_after_seconds,
            )
        except asyncio.CancelledError:
            if target is not None:
                await self.storage.discard(target)
            with contextlib.suppress(Exception):
                await self._retry(job, worker_id, "worker stopped", retryable=True)
            raise
        # A provider is an external adapter; isolate unknown adapter failures from the pool.
        except Exception as error:  # noqa: BLE001
            logger.error(
                "Unexpected %s while processing job %s",
                error.__class__.__name__,
                job.id,
            )
            if target is not None:
                with contextlib.suppress(Exception):
                    await self.storage.discard(target)
            await self._retry(
                job,
                worker_id,
                f"unexpected {error.__class__.__name__}",
                retryable=True,
            )

    async def _download_with_cancellation(
        self,
        provider: DownloadProvider,
        job: DownloadJob,
        target: StorageTarget,
        progress: _ProgressReporter,
        worker_id: str,
    ) -> None:
        task = asyncio.create_task(
            provider.download(job, target.temporary_path, progress),
            name=f"download-{job.id}",
        )
        try:
            while not task.done():
                done, _ = await asyncio.wait(
                    {task}, timeout=self.settings.cancellation_poll_seconds
                )
                if done:
                    break
                if await self.queue.is_cancel_requested(job.id, worker_id):
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                    raise JobCancellationRequested
            await task
            await progress.flush()
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def _retry(
        self,
        job: DownloadJob,
        worker_id: str,
        error: str,
        *,
        retryable: bool,
        retry_after_seconds: float = 0,
    ) -> None:
        backoff = self.settings.retry_base_seconds * (2 ** max(0, job.attempt_count - 1))
        updated = await self.queue.fail_or_retry(
            job.id,
            worker_id,
            error,
            retryable=retryable,
            retry_delay_seconds=max(backoff, retry_after_seconds) if retryable else 0,
        )
        if updated.is_terminal:
            await self._publish(updated)

    async def _publish(self, job: DownloadJob) -> None:
        if self.event_sink is None:
            return
        try:
            await self.event_sink.job_finished(job)
        except Exception as error:  # noqa: BLE001 - notifications cannot change job state
            logger.warning(
                "Ignoring %s while notifying for job %s",
                error.__class__.__name__,
                job.id,
            )


class WorkerPool:
    def __init__(self, worker: DownloadWorker, concurrency: int) -> None:
        if concurrency <= 0:
            raise ValueError("concurrency must be positive")
        self.worker = worker
        self.concurrency = concurrency
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        if self._tasks:
            raise RuntimeError("worker pool is already started")
        self._tasks = [
            asyncio.create_task(self._run_loop(index), name=f"tgharbor-worker-{index}")
            for index in range(self.concurrency)
        ]

    def notify(self) -> None:
        self._wake.set()

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_loop(self, index: int) -> None:
        worker_id = f"worker-{index}"
        while True:
            processed = await self.worker.run_once(worker_id)
            if processed:
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self.worker.settings.idle_poll_seconds
                )
            except TimeoutError:
                pass


class _ProgressReporter:
    def __init__(
        self,
        queue: JobQueue,
        job: DownloadJob,
        worker_id: str,
        *,
        interval_seconds: float,
    ) -> None:
        self.queue = queue
        self.job = job
        self.worker_id = worker_id
        self.interval_seconds = interval_seconds
        self.current = 0
        self.total: int | None = None
        self._last_written = 0.0

    async def __call__(self, current: int, total: int | None) -> None:
        self.current = current
        self.total = total
        now = time.monotonic()
        if now - self._last_written >= self.interval_seconds or (
            total is not None and current >= total
        ):
            await self._write(now)

    async def flush(self) -> None:
        await self._write(time.monotonic())

    async def _write(self, now: float) -> None:
        await self.queue.update_progress(self.job.id, self.worker_id, self.current, self.total)
        self._last_written = now


def _safe_error(error: BaseException) -> str:
    text = " ".join(str(error).split()) or error.__class__.__name__
    return text[:500]
