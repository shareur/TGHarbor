from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from tgharbor.core.errors import (
    EXCEPTION_TYPE_NAME_FALLBACK,
    DownloadError,
    JobCancellationRequested,
    PersistenceError,
    download_error_detail,
    safe_exception_type_name,
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

# Repeated transient persistence failures of one fingerprint are logged once
# immediately, then at most once per interval with only a suppressed count.
PERSISTENCE_LOG_AGGREGATE_SECONDS: Final = 60.0


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
                job = await self.queue.update_metadata(
                    job.id,
                    worker_id,
                    display_title=descriptor.display_title,
                    original_file_name=descriptor.file_name,
                    mime_type=descriptor.mime_type,
                    total_bytes=descriptor.total_bytes,
                    origin_group_id=descriptor.origin_group_id,
                    channel_id=descriptor.channel_id,
                )
                desired_name = self.storage.normalize_name(
                    job.destination_name or descriptor.file_name
                )
                job = await self.queue.reserve_name(job.id, worker_id, desired_name)
                target = await self.storage.prepare(job, desired_name)
                if self.storage.is_complete(target):
                    completed = await self.queue.complete(
                        job.id,
                        worker_id,
                        target.final_path,
                        relative_path=self.storage.relative_path(target.final_path),
                    )
                    await self._publish(completed, worker_id)
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
                completed = await self.queue.complete(
                    job.id,
                    worker_id,
                    output,
                    relative_path=self.storage.relative_path(output),
                )
                await self._publish(completed, worker_id)
        except JobCancellationRequested:
            if target is not None:
                await self.storage.discard(target)
            cancelled = await self.queue.mark_cancelled(job.id, worker_id)
            await self._publish(cancelled, worker_id)
        except TimeoutError:
            if target is not None:
                await self.storage.discard(target)
            await self._retry(job, worker_id, "download timed out", retryable=True)
        except DownloadError as error:
            if target is not None:
                await self.storage.discard(target)
            logger.warning(
                "Download failure %s while processing job %s on %s",
                safe_exception_type_name(error, fallback=DownloadError.__name__),
                job.id,
                worker_id,
            )
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
        except PersistenceError as error:
            # Log only the sanitized contract fields; never render a chained cause.
            logger.error(
                "Persistence failure category=%s error_type=%s code=%s while processing "
                "job %s on %s",
                error.category,
                error.error_type,
                error.code,
                job.id,
                worker_id,
            )
            if target is not None:
                with contextlib.suppress(Exception):
                    await self.storage.discard(target)
            await self._retry(
                job,
                worker_id,
                f"database {error.category}",
                retryable=error.retryable,
            )
        # A provider is an external adapter; isolate unknown adapter failures from the pool.
        except Exception as error:  # noqa: BLE001
            error_type = safe_exception_type_name(error, fallback=EXCEPTION_TYPE_NAME_FALLBACK)
            logger.error(
                "Unexpected %s while processing job %s",
                error_type,
                job.id,
            )
            if target is not None:
                with contextlib.suppress(Exception):
                    await self.storage.discard(target)
            await self._retry(
                job,
                worker_id,
                f"unexpected {error_type}",
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
            await self._publish(updated, worker_id)

    async def _publish(self, job: DownloadJob, worker_id: str) -> None:
        if self.event_sink is None:
            return
        try:
            await self.event_sink.job_finished(job)
        except PersistenceError as error:
            logger.warning(
                "Persistence notification failure category=%s error_type=%s code=%s for "
                "job %s on %s",
                error.category,
                error.error_type,
                error.code,
                job.id,
                worker_id,
            )
        except Exception as error:  # noqa: BLE001 - notifications cannot change job state
            logger.warning(
                "Ignoring %s while notifying for job %s",
                safe_exception_type_name(error, fallback=EXCEPTION_TYPE_NAME_FALLBACK),
                job.id,
            )


@dataclass(frozen=True, slots=True)
class PersistenceLogEvent:
    """A sanitized worker-pool persistence diagnostic.

    Only the stable category, the bounded exception class name, the numeric code,
    and an aggregate count are retained. Raw messages, causes, SQL, parameters,
    paths, and provider or Telegram data are never stored.
    """

    kind: str
    category: str
    error_type: str
    code: int | None
    suppressed: int = 0

    def render(self, worker_id: str) -> tuple[str, tuple[object, ...]]:
        """Return a log template and its sanitized arguments."""

        fingerprint: tuple[object, ...] = (worker_id, self.category, self.error_type, self.code)
        if self.kind == "aggregate":
            template = (
                "Worker %s persistence failure continuing category=%s error_type=%s "
                "code=%s suppressed=%d"
            )
            return template, (*fingerprint, self.suppressed)
        if self.kind == "recovered":
            template = (
                "Worker %s persistence failure recovered category=%s error_type=%s "
                "code=%s suppressed=%d"
            )
            return template, (*fingerprint, self.suppressed)
        template = "Worker %s persistence failure category=%s error_type=%s code=%s"
        return template, fingerprint


class PersistenceFailureLogThrottle:
    """Rate-limit repeated transient persistence diagnostics per fingerprint.

    The first occurrence of a fingerprint is reported immediately. Identical
    repeats are counted and reported at most once per ``aggregate_seconds`` with
    only the suppressed count, and one recovery event is produced on the next
    successful iteration. Time comes from an injectable monotonic clock so tests
    are deterministic. Retry, backoff, and job semantics are untouched.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        aggregate_seconds: float = PERSISTENCE_LOG_AGGREGATE_SECONDS,
    ) -> None:
        if aggregate_seconds <= 0:
            raise ValueError("aggregate_seconds must be positive")
        self._clock = clock
        self._aggregate_seconds = aggregate_seconds
        self._fingerprint: tuple[str, str, int | None] | None = None
        self._last_logged = 0.0
        self._suppressed = 0

    @property
    def active_fingerprint(self) -> tuple[str, str, int | None] | None:
        return self._fingerprint

    def failure(self, error: PersistenceError) -> PersistenceLogEvent | None:
        """Record one transient failure and return an event when it should log."""

        fingerprint = (error.category, error.error_type, error.code)
        now = self._clock()
        if fingerprint != self._fingerprint:
            self._fingerprint = fingerprint
            self._last_logged = now
            self._suppressed = 0
            return PersistenceLogEvent("failure", *fingerprint)
        if now - self._last_logged >= self._aggregate_seconds:
            suppressed, self._suppressed = self._suppressed, 0
            self._last_logged = now
            return PersistenceLogEvent("aggregate", *fingerprint, suppressed=suppressed)
        self._suppressed += 1
        return None

    def recovered(self) -> PersistenceLogEvent | None:
        """Return one recovery event after a success, or ``None`` when healthy."""

        fingerprint = self._fingerprint
        if fingerprint is None:
            return None
        suppressed = self._suppressed
        self._fingerprint = None
        self._last_logged = 0.0
        self._suppressed = 0
        return PersistenceLogEvent("recovered", *fingerprint, suppressed=suppressed)


class WorkerPool:
    def __init__(
        self,
        worker: DownloadWorker,
        concurrency: int,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if concurrency <= 0:
            raise ValueError("concurrency must be positive")
        self.worker = worker
        self.concurrency = concurrency
        self._clock = time.monotonic if clock is None else clock
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
        reported_permanent_failure: tuple[str, str, int | None] | None = None
        transient_logs = PersistenceFailureLogThrottle(clock=self._clock)
        while True:
            try:
                processed = await self.worker.run_once(worker_id)
            except asyncio.CancelledError:
                raise
            except PersistenceError as error:
                fingerprint = (error.category, error.error_type, error.code)
                if error.retryable:
                    # A transient failure keeps the existing retry and idle
                    # cadence; only its diagnostics are rate limited.
                    event = transient_logs.failure(error)
                    if event is not None:
                        message, arguments = event.render(worker_id)
                        logger.error(message, *arguments)
                    reported_permanent_failure = None
                else:
                    # A permanent failure cannot be fixed by re-claiming. Keep the
                    # existing idle cadence but log only the first occurrence of an
                    # identical permanent fingerprint so the loop cannot flood logs.
                    if fingerprint != reported_permanent_failure:
                        message, arguments = PersistenceLogEvent("failure", *fingerprint).render(
                            worker_id
                        )
                        logger.error(message, *arguments)
                    reported_permanent_failure = fingerprint
                processed = False
            else:
                reported_permanent_failure = None
                recovery = transient_logs.recovered()
                if recovery is not None:
                    message, arguments = recovery.render(worker_id)
                    logger.info(message, *arguments)
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


def _safe_error(error: DownloadError) -> str:
    return download_error_detail(error)
