from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path

import pytest

from tgharbor.core.errors import (
    EXCEPTION_TYPE_NAME_MAX_LENGTH,
    PERMANENT_PERSISTENCE_CATEGORIES,
    PERSISTENCE_DETAILS,
    PUBLIC_DOWNLOAD_ERROR_DETAILS,
    TRANSIENT_PERSISTENCE_CATEGORIES,
    PermanentDownloadError,
    PersistenceError,
    RetryableDownloadError,
    download_error_detail,
    safe_exception_type_name,
)
from tgharbor.core.interfaces import MediaDescriptor, ProgressCallback
from tgharbor.core.jobs import DownloadJob, JobStatus, NewDownloadJob, SourceSession
from tgharbor.core.worker import (
    PERSISTENCE_LOG_AGGREGATE_SECONDS,
    DownloadWorker,
    PersistenceFailureLogThrottle,
    WorkerPool,
    WorkerSettings,
)
from tgharbor.persistence import SQLiteJobQueue
from tgharbor.persistence.sqlite_queue import _translate_persistence_error
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


class ChainedFailingProvider(SuccessfulProvider):
    def __init__(self, message: str, cause: BaseException) -> None:
        super().__init__()
        self.message = message
        self.cause = cause

    async def download(
        self, job: DownloadJob, destination: Path, progress: ProgressCallback
    ) -> None:
        self.download_count += 1
        raise PermanentDownloadError(self.message) from self.cause


class FailingNotifier:
    async def job_finished(self, job: DownloadJob) -> None:
        raise OSError("notification unavailable")


class PersistenceFailingNotifier:
    async def job_finished(self, job: DownloadJob) -> None:
        raise persistence_error("io", error_type="OperationalError")


class FlakyClaimQueue:
    def __init__(self, queue: SQLiteJobQueue, *, failures: int = 1) -> None:
        self._queue = queue
        self.failures = failures

    async def claim_next(self, worker_id: str, *, now: float | None = None) -> DownloadJob | None:
        if self.failures:
            self.failures -= 1
            raise persistence_error("busy")
        return await self._queue.claim_next(worker_id, now=now)

    def __getattr__(self, name: str) -> object:
        return getattr(self._queue, name)


class AlwaysFailingClaimQueue:
    def __init__(self, error: PersistenceError) -> None:
        self.error = error
        self.attempts = 0

    async def claim_next(self, worker_id: str, *, now: float | None = None) -> DownloadJob | None:
        self.attempts += 1
        raise self.error


class FinalizeFailingQueue:
    def __init__(self, queue: SQLiteJobQueue) -> None:
        self._queue = queue

    async def complete(self, *args: object, **kwargs: object) -> DownloadJob:
        raise persistence_error("database")

    async def fail_or_retry(self, *args: object, **kwargs: object) -> DownloadJob:
        raise persistence_error("database")

    def __getattr__(self, name: str) -> object:
        return getattr(self._queue, name)


class FakeClock:
    """A deterministic stand-in for ``time.monotonic``."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def persistence_error(
    category: str,
    *,
    error_type: str = "OperationalError",
    code: int | None = 5,
) -> PersistenceError:
    return PersistenceError(
        category,
        PERSISTENCE_DETAILS[category],
        error_type=error_type,
        code=code,
    )


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
    provider = FailingProvider(
        RetryableDownloadError("Telegram rate limit", retry_after_seconds=60)
    )
    queue, _, worker = await components(tmp_path, provider)
    pending = await enqueue(queue, 2)

    await worker.run_once("worker")

    retried = await queue.get(pending.id)
    assert retried is not None and retried.status is JobStatus.PENDING
    assert retried.error == "Telegram rate limit"
    assert (retried.available_at - retried.updated_at).total_seconds() >= 59


async def test_permanent_provider_error_fails_immediately(tmp_path: Path) -> None:
    provider = FailingProvider(PermanentDownloadError("Telegram message has no downloadable media"))
    queue, _, worker = await components(tmp_path, provider)
    pending = await enqueue(queue, 3)

    await worker.run_once("worker")

    failed = await queue.get(pending.id)
    assert failed is not None and failed.status is JobStatus.FAILED
    assert failed.attempt_count == 1
    assert failed.error == "Telegram message has no downloadable media"


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
    assert await queue.continue_paused_for_requester(1001) == 1

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


async def test_persistence_failure_requeues_and_logs_sanitized_fields(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    provider = FailingProvider(persistence_error("busy"))
    queue, _, worker = await components(tmp_path, provider)
    pending = await enqueue(queue, 20)

    with caplog.at_level(logging.ERROR):
        await worker.run_once("worker")

    retried = await queue.get(pending.id)
    assert retried is not None and retried.status is JobStatus.PENDING
    assert retried.error == "database busy"
    assert "category=busy" in caplog.text
    assert "error_type=OperationalError" in caplog.text
    assert "code=5" in caplog.text
    assert "Traceback" not in caplog.text
    assert "database is locked" not in caplog.text


async def test_persistence_diagnostics_exclude_sensitive_values(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sentinels = (
        "https://t.me/private/987654",
        "BOT_TOKEN_SENTINEL_123",
        "SECRET_FILENAME_SENTINEL.mkv",
        "/srv/private/state/tgharbor.db",
    )
    raw_error = sqlite3.OperationalError(
        f"unable to open database file {sentinels[3]} token={sentinels[1]} "
        f"file={sentinels[2]} source={sentinels[0]}"
    )
    provider = FailingProvider(_translate_persistence_error(raw_error))
    queue, _, worker = await components(tmp_path, provider)
    pending = await enqueue(queue, 21)

    with caplog.at_level(logging.ERROR):
        await worker.run_once("worker")

    retried = await queue.get(pending.id)
    assert retried is not None and retried.error == "database open"
    for sentinel in sentinels:
        assert sentinel not in caplog.text
        assert sentinel not in (retried.error or "")


async def test_generic_provider_error_logs_class_name_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    provider = FailingProvider(
        RuntimeError("token=SECRET_RUNTIME_TOKEN https://t.me/private/1 SECRET_RUNTIME_FILE.mkv")
    )
    queue, _, worker = await components(tmp_path, provider)
    pending = await enqueue(queue, 22)

    with caplog.at_level(logging.ERROR):
        await worker.run_once("worker")

    retried = await queue.get(pending.id)
    assert retried is not None and retried.error == "unexpected RuntimeError"
    assert "RuntimeError" in caplog.text
    assert "SECRET_RUNTIME_TOKEN" not in caplog.text
    assert "SECRET_RUNTIME_FILE.mkv" not in caplog.text
    assert "https://t.me/private/1" not in caplog.text


async def test_generic_provider_error_with_malformed_class_name_uses_fixed_fallback(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    malformed_name = "Malformed /private/state " + "X" * (EXCEPTION_TYPE_NAME_MAX_LENGTH + 20)
    hostile_type = type(malformed_name, (RuntimeError,), {})
    message_secret = "GENERIC_MESSAGE_SENTINEL /srv/private"
    provider = FailingProvider(hostile_type(message_secret))
    queue, _, worker = await components(tmp_path, provider)
    pending = await enqueue(queue, 27)

    with caplog.at_level(logging.ERROR):
        await worker.run_once("worker")

    retried = await queue.get(pending.id)
    assert retried is not None and retried.error == "unexpected Exception"
    assert "Unexpected Exception" in caplog.text
    assert malformed_name not in caplog.text
    assert malformed_name not in (retried.error or "")
    assert message_secret not in caplog.text
    assert message_secret not in (retried.error or "")


async def test_notification_persistence_failure_leaves_job_completed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    provider = SuccessfulProvider()
    queue, storage, _ = await components(tmp_path, provider)
    worker = DownloadWorker(
        queue,
        {SourceSession.BOT: provider},
        storage,
        WorkerSettings(timeout_seconds=1, retry_base_seconds=1),
        PersistenceFailingNotifier(),
    )
    pending = await enqueue(queue, 23)

    with caplog.at_level(logging.WARNING):
        await worker.run_once("worker")

    completed = await queue.get(pending.id)
    assert completed is not None and completed.status is JobStatus.COMPLETED
    assert "Persistence notification failure category=io" in caplog.text


async def test_worker_pool_survives_claim_persistence_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    provider = SuccessfulProvider()
    queue, storage, _ = await components(tmp_path, provider)
    pending = await enqueue(queue, 24)
    flaky_queue = FlakyClaimQueue(queue)
    worker = DownloadWorker(
        flaky_queue,
        {SourceSession.BOT: provider},
        storage,
        WorkerSettings(
            timeout_seconds=1,
            retry_base_seconds=0.001,
            progress_interval_seconds=0,
            cancellation_poll_seconds=0.005,
            idle_poll_seconds=0.01,
        ),
    )
    pool = WorkerPool(worker, 1)

    with caplog.at_level(logging.ERROR):
        await pool.start()
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 2
            while True:
                completed = await queue.get(pending.id)
                if completed is not None and completed.status is JobStatus.COMPLETED:
                    break
                if loop.time() > deadline:
                    pytest.fail("worker pool did not continue after a claim failure")
                await asyncio.sleep(0.01)
        finally:
            await pool.stop()

    assert flaky_queue.failures == 0
    assert "Worker worker-0 persistence failure" in caplog.text
    assert "category=busy" in caplog.text


@pytest.mark.parametrize("message", sorted(PUBLIC_DOWNLOAD_ERROR_DETAILS))
def test_allowlisted_provider_details_are_preserved(message: str) -> None:
    assert download_error_detail(RetryableDownloadError(message)) == message


def test_safe_exception_type_name_accepts_only_bounded_identifiers() -> None:
    assert safe_exception_type_name(RuntimeError("secret"), fallback="Exception") == "RuntimeError"
    assert safe_exception_type_name(RuntimeError, fallback="Exception") == "RuntimeError"
    assert safe_exception_type_name("RuntimeError", fallback="Exception") == "RuntimeError"
    assert safe_exception_type_name("not an identifier", fallback="Exception") == "Exception"
    assert (
        safe_exception_type_name("A" * (EXCEPTION_TYPE_NAME_MAX_LENGTH + 1), fallback="Exception")
        == "Exception"
    )


def test_download_error_detail_requires_an_exact_allowlist_match() -> None:
    normalized = RetryableDownloadError("  Telegram\t rate\nlimit ")
    assert download_error_detail(normalized) == "Telegram rate limit"

    prefixed = PermanentDownloadError(
        "Telegram rate limit; token=PROVIDER_DETAIL_SENTINEL /private/path"
    )
    assert download_error_detail(prefixed) == "PermanentDownloadError"

    hostile_type = type("SECRET CLASS /private", (PermanentDownloadError,), {})
    hostile = hostile_type("SECRET MESSAGE")
    assert download_error_detail(hostile) == "DownloadError"


async def test_custom_provider_error_with_chained_cause_is_class_name_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    message_secret = "token=PROVIDER_MESSAGE_SENTINEL https://t.me/private/2"
    cause_secret = "CAUSE_SENTINEL /srv/private/provider-path"
    provider = ChainedFailingProvider(message_secret, RuntimeError(cause_secret))
    queue, _, worker = await components(tmp_path, provider)
    pending = await enqueue(queue, 25)

    with caplog.at_level(logging.WARNING):
        await worker.run_once("worker")

    failed = await queue.get(pending.id)
    assert failed is not None and failed.status is JobStatus.FAILED
    assert failed.error == "PermanentDownloadError"
    assert "PermanentDownloadError" in caplog.text
    assert message_secret not in caplog.text
    assert cause_secret not in caplog.text
    assert message_secret not in (failed.error or "")


async def test_persistence_failure_does_not_render_a_chained_cause(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cause_secret = "CHAINED_PERSISTENCE_SENTINEL /private/state"
    error = persistence_error("busy")
    error.__cause__ = RuntimeError(cause_secret)
    provider = FailingProvider(error)
    queue, _, worker = await components(tmp_path, provider)
    pending = await enqueue(queue, 26)

    with caplog.at_level(logging.ERROR):
        await worker.run_once("worker")

    retried = await queue.get(pending.id)
    assert retried is not None and retried.error == "database busy"
    assert "category=busy" in caplog.text
    assert cause_secret not in caplog.text
    assert "RuntimeError" not in caplog.text


@pytest.mark.parametrize("category", sorted(TRANSIENT_PERSISTENCE_CATEGORIES))
async def test_transient_persistence_category_requeues(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, category: str
) -> None:
    provider = FailingProvider(persistence_error(category))
    queue, _, worker = await components(tmp_path, provider)
    pending = await enqueue(queue, 30)

    with caplog.at_level(logging.ERROR):
        await worker.run_once("worker")

    retried = await queue.get(pending.id)
    assert retried is not None and retried.status is JobStatus.PENDING
    assert retried.attempt_count == 1
    assert retried.error == f"database {category}"
    assert f"category={category}" in caplog.text


@pytest.mark.parametrize("category", sorted(PERMANENT_PERSISTENCE_CATEGORIES))
async def test_permanent_persistence_category_fails_without_retry(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, category: str
) -> None:
    provider = FailingProvider(persistence_error(category))
    queue, _, worker = await components(tmp_path, provider)
    pending = await enqueue(queue, 31)

    with caplog.at_level(logging.ERROR):
        await worker.run_once("worker")

    failed = await queue.get(pending.id)
    assert failed is not None and failed.status is JobStatus.FAILED
    assert failed.attempt_count == 1
    assert failed.error == f"database {category}"
    assert f"category={category}" in caplog.text


async def test_worker_pool_suppresses_repeated_permanent_claim_failure_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    provider = SuccessfulProvider()
    _, storage, _ = await components(tmp_path, provider)
    failing_queue = AlwaysFailingClaimQueue(persistence_error("corrupt"))
    worker = DownloadWorker(
        failing_queue,
        {SourceSession.BOT: provider},
        storage,
        WorkerSettings(
            timeout_seconds=1,
            retry_base_seconds=0.001,
            progress_interval_seconds=0,
            cancellation_poll_seconds=0.005,
            idle_poll_seconds=0.01,
        ),
    )
    pool = WorkerPool(worker, 1)

    with caplog.at_level(logging.ERROR):
        await pool.start()
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 2
            while failing_queue.attempts < 5:
                if loop.time() > deadline:
                    pytest.fail("worker pool did not keep polling after a permanent failure")
                await asyncio.sleep(0.01)
            assert all(not task.done() for task in pool._tasks)
        finally:
            await pool.stop()

    messages = [
        record.getMessage() for record in caplog.records if record.name == "tgharbor.core.worker"
    ]
    assert messages == [
        "Worker worker-0 persistence failure category=corrupt error_type=OperationalError code=5"
    ]


async def test_finalize_persistence_failure_uses_neutral_wording_and_strands_job(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    provider = SuccessfulProvider()
    queue, storage, _ = await components(tmp_path, provider)
    pending = await enqueue(queue, 32)
    finalize_queue = FinalizeFailingQueue(queue)
    worker = DownloadWorker(
        finalize_queue,
        {SourceSession.BOT: provider},
        storage,
        WorkerSettings(
            timeout_seconds=1,
            retry_base_seconds=0.001,
            progress_interval_seconds=0,
            cancellation_poll_seconds=0.005,
            idle_poll_seconds=0.01,
        ),
    )
    pool = WorkerPool(worker, 1)

    with caplog.at_level(logging.ERROR):
        await pool.start()
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 2
            while "Worker worker-0 persistence failure" not in caplog.text:
                if loop.time() > deadline:
                    pytest.fail("finalize persistence failure was not reported")
                await asyncio.sleep(0.01)
        finally:
            await pool.stop()

    stranded = await queue.get(pending.id)
    assert stranded is not None and stranded.status is JobStatus.RUNNING
    assert "category=database" in caplog.text
    assert "claiming" not in caplog.text


@pytest.mark.parametrize("value", ["Érror", "Ошибка", "Ｅｒｒｏｒ", "Errоr"])
def test_safe_exception_type_name_rejects_unicode_identifiers(value: str) -> None:
    assert value.isidentifier()
    assert not value.isascii()

    assert safe_exception_type_name(value, fallback="Exception") == "Exception"
    assert safe_exception_type_name(RuntimeError, fallback="Exception") == "RuntimeError"


@pytest.mark.parametrize(
    "value",
    [
        "Error\u200b",
        "Ошибка!",
        "Ｅｒｒｏｒ Ｔｙｐｅ",
        "Δt",
        "Ⅳ",
    ],
)
def test_safe_exception_type_name_rejects_adversarial_text(value: str) -> None:
    assert safe_exception_type_name(value, fallback="Exception") == "Exception"


def test_persistence_error_rejects_a_non_ascii_error_type() -> None:
    with pytest.raises(ValueError):
        PersistenceError("busy", PERSISTENCE_DETAILS["busy"], error_type="Ошибка")


def test_unicode_exception_class_name_is_not_stored() -> None:
    hostile_type = type("Ошибка", (PermanentDownloadError,), {})

    assert download_error_detail(hostile_type("SECRET MESSAGE")) == "DownloadError"


def test_persistence_log_throttle_logs_the_first_failure_immediately() -> None:
    clock = FakeClock()
    throttle = PersistenceFailureLogThrottle(clock=clock)

    event = throttle.failure(persistence_error("busy"))

    assert event is not None
    assert (event.kind, event.category, event.error_type, event.code, event.suppressed) == (
        "failure",
        "busy",
        "OperationalError",
        5,
        0,
    )
    message, arguments = event.render("worker-0")
    assert message % arguments == (
        "Worker worker-0 persistence failure category=busy error_type=OperationalError code=5"
    )


def test_persistence_log_throttle_suppresses_identical_failures_within_interval() -> None:
    clock = FakeClock()
    throttle = PersistenceFailureLogThrottle(clock=clock)
    error = persistence_error("busy")

    assert throttle.failure(error) is not None
    for _ in range(5):
        assert throttle.failure(error) is None

    clock.advance(PERSISTENCE_LOG_AGGREGATE_SECONDS - 1)
    assert throttle.failure(error) is None


def test_persistence_log_throttle_emits_one_aggregate_per_interval() -> None:
    clock = FakeClock()
    throttle = PersistenceFailureLogThrottle(clock=clock)
    error = persistence_error("busy")

    assert throttle.failure(error) is not None
    for _ in range(3):
        assert throttle.failure(error) is None
    clock.advance(PERSISTENCE_LOG_AGGREGATE_SECONDS - 1)
    assert throttle.failure(error) is None

    clock.advance(1)
    aggregate = throttle.failure(error)

    assert aggregate is not None
    assert (aggregate.kind, aggregate.suppressed) == ("aggregate", 4)
    message, arguments = aggregate.render("worker-1")
    assert message % arguments == (
        "Worker worker-1 persistence failure continuing category=busy "
        "error_type=OperationalError code=5 suppressed=4"
    )

    assert throttle.failure(error) is None
    clock.advance(PERSISTENCE_LOG_AGGREGATE_SECONDS)
    later = throttle.failure(error)
    assert later is not None
    assert (later.kind, later.suppressed) == ("aggregate", 1)


def test_persistence_log_throttle_reports_a_fingerprint_change_immediately() -> None:
    clock = FakeClock()
    throttle = PersistenceFailureLogThrottle(clock=clock)

    assert throttle.failure(persistence_error("busy", code=5)) is not None
    assert throttle.failure(persistence_error("busy", code=5)) is None

    changed = throttle.failure(persistence_error("busy", code=7))

    assert changed is not None
    assert (changed.kind, changed.code, changed.suppressed) == ("failure", 7, 0)


def test_persistence_log_throttle_emits_one_recovery_message() -> None:
    clock = FakeClock()
    throttle = PersistenceFailureLogThrottle(clock=clock)
    error = persistence_error("io")

    assert throttle.failure(error) is not None
    for _ in range(2):
        assert throttle.failure(error) is None

    recovery = throttle.recovered()

    assert recovery is not None
    assert (recovery.kind, recovery.category, recovery.suppressed) == ("recovered", "io", 2)
    message, arguments = recovery.render("worker-2")
    assert message % arguments == (
        "Worker worker-2 persistence failure recovered category=io "
        "error_type=OperationalError code=5 suppressed=2"
    )
    assert throttle.recovered() is None


def test_persistence_log_throttle_is_silent_while_healthy() -> None:
    throttle = PersistenceFailureLogThrottle(clock=FakeClock())

    assert throttle.recovered() is None
    assert throttle.active_fingerprint is None


def test_persistence_log_events_contain_only_sanitized_fields() -> None:
    clock = FakeClock()
    throttle = PersistenceFailureLogThrottle(clock=clock)

    event = throttle.failure(persistence_error("disk-full"))
    assert event is not None
    message, arguments = event.render("worker-9")
    rendered = message % arguments

    assert rendered == (
        "Worker worker-9 persistence failure category=disk-full error_type=OperationalError code=5"
    )
    assert "SELECT" not in rendered
    assert "/" not in rendered
    assert "sqlite" not in rendered.casefold()


async def test_worker_pool_aggregates_repeated_transient_failure_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    provider = SuccessfulProvider()
    _, storage, _ = await components(tmp_path, provider)
    clock = FakeClock()
    failing_queue = AlwaysFailingClaimQueue(persistence_error("busy"))
    worker = DownloadWorker(
        failing_queue,
        {SourceSession.BOT: provider},
        storage,
        WorkerSettings(
            timeout_seconds=1,
            retry_base_seconds=0.001,
            progress_interval_seconds=0,
            cancellation_poll_seconds=0.005,
            idle_poll_seconds=0.01,
        ),
    )
    pool = WorkerPool(worker, 1, clock=clock)

    def messages() -> list[str]:
        return [
            record.getMessage()
            for record in caplog.records
            if record.name == "tgharbor.core.worker"
        ]

    with caplog.at_level(logging.ERROR):
        await pool.start()
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 2
            while failing_queue.attempts < 4:
                if loop.time() > deadline:
                    pytest.fail("worker pool did not keep polling after a transient failure")
                await asyncio.sleep(0.01)
            assert messages() == [
                (
                    "Worker worker-0 persistence failure category=busy "
                    "error_type=OperationalError code=5"
                )
            ]

            clock.advance(PERSISTENCE_LOG_AGGREGATE_SECONDS)
            deadline = loop.time() + 2
            while len(messages()) < 2:
                if loop.time() > deadline:
                    pytest.fail("worker pool did not emit an aggregate persistence log")
                await asyncio.sleep(0.01)
            assert all(not task.done() for task in pool._tasks)
        finally:
            await pool.stop()

    assert messages()[1].startswith(
        "Worker worker-0 persistence failure continuing category=busy "
        "error_type=OperationalError code=5 suppressed="
    )
    assert len(messages()) == 2


async def test_worker_pool_logs_one_recovery_after_a_transient_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    provider = SuccessfulProvider()
    queue, storage, _ = await components(tmp_path, provider)
    pending = await enqueue(queue, 33)
    flaky_queue = FlakyClaimQueue(queue)
    worker = DownloadWorker(
        flaky_queue,
        {SourceSession.BOT: provider},
        storage,
        WorkerSettings(
            timeout_seconds=1,
            retry_base_seconds=0.001,
            progress_interval_seconds=0,
            cancellation_poll_seconds=0.005,
            idle_poll_seconds=0.01,
        ),
    )
    pool = WorkerPool(worker, 1)

    def messages() -> list[str]:
        return [
            record.getMessage()
            for record in caplog.records
            if record.name == "tgharbor.core.worker"
        ]

    with caplog.at_level(logging.INFO):
        await pool.start()
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 2
            while True:
                completed = await queue.get(pending.id)
                if (
                    completed is not None
                    and completed.status is JobStatus.COMPLETED
                    and any("recovered" in message for message in messages())
                ):
                    break
                if loop.time() > deadline:
                    pytest.fail("worker pool did not report persistence recovery")
                await asyncio.sleep(0.01)
        finally:
            await pool.stop()

    assert messages() == [
        "Worker worker-0 persistence failure category=busy error_type=OperationalError code=5",
        (
            "Worker worker-0 persistence failure recovered category=busy "
            "error_type=OperationalError code=5 suppressed=0"
        ),
    ]
