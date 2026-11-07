from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from tgharbor.core.jobs import FileLocation, JobStatus, NewDownloadJob, SourceSession
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


async def create_v1_database(path: Path, request: NewDownloadJob) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = request.created_at.timestamp()
    async with aiosqlite.connect(path) as connection:
        await connection.executescript(
            """
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                requester_id INTEGER NOT NULL,
                source_session TEXT NOT NULL,
                source_chat TEXT NOT NULL,
                source_message_id INTEGER NOT NULL,
                destination_name TEXT,
                dedupe_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL,
                progress_bytes INTEGER NOT NULL DEFAULT 0,
                total_bytes INTEGER,
                output_path TEXT,
                error TEXT,
                available_at REAL NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                claimed_by TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX jobs_claimable_idx
                ON jobs(status, available_at, created_at, id);
            CREATE INDEX jobs_requester_idx
                ON jobs(requester_id, created_at DESC);
            PRAGMA user_version = 1;
            """
        )
        await connection.execute(
            """
            INSERT INTO jobs (
                id, requester_id, source_session, source_chat, source_message_id,
                destination_name, dedupe_key, status, max_attempts,
                available_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(request.id),
                request.requester_id,
                request.source_session.value,
                request.source_chat,
                request.source_message_id,
                request.destination_name,
                request.dedupe_key,
                JobStatus.PENDING.value,
                request.max_attempts,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        await connection.commit()


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

    async with aiosqlite.connect(queue.path) as connection:
        await connection.execute("PRAGMA user_version = 999")
        await connection.commit()

    with pytest.raises(RuntimeError, match="newer than supported"):
        await queue.initialize()


async def test_v1_database_migrates_transactionally_and_preserves_jobs(tmp_path: Path) -> None:
    request = new_job(60)
    database = tmp_path / "state/tgharbor.db"
    await create_v1_database(database, request)

    queue = SQLiteJobQueue(database)
    await queue.initialize()

    migrated = await queue.get(request.id)
    assert migrated is not None
    assert migrated.status is JobStatus.PENDING
    assert migrated.display_title is None
    assert migrated.original_file_name is None
    assert migrated.mime_type is None
    assert migrated.reserved_name is None
    await queue.record_directory_creation(2002, Path("archive/videos"))

    async with aiosqlite.connect(database) as connection:
        version = await (await connection.execute("PRAGMA user_version")).fetchone()
        columns = await (await connection.execute("PRAGMA table_info(jobs)")).fetchall()
        tables = await (
            await connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ).fetchall()
        audit = await (
            await connection.execute(
                "SELECT job_id, operator_id, action, destination_path FROM file_audit"
            )
        ).fetchone()

    assert version == (2,)
    assert {column[1] for column in columns} >= {
        "display_title",
        "original_file_name",
        "mime_type",
        "reserved_name",
    }
    assert {table[0] for table in tables} >= {"stored_files", "file_audit", "interactions"}
    assert audit == (None, 2002, "mkdir", "archive/videos")


async def test_failed_v1_migration_rolls_back_schema_changes(tmp_path: Path) -> None:
    request = new_job(61)
    database = tmp_path / "state/tgharbor.db"
    await create_v1_database(database, request)
    async with aiosqlite.connect(database) as connection:
        await connection.execute("CREATE TABLE stored_files (sentinel TEXT)")
        await connection.commit()

    with pytest.raises(aiosqlite.OperationalError, match="already exists"):
        await SQLiteJobQueue(database).initialize()

    async with aiosqlite.connect(database) as connection:
        version = await (await connection.execute("PRAGMA user_version")).fetchone()
        columns = await (await connection.execute("PRAGMA table_info(jobs)")).fetchall()

    assert version == (1,)
    assert "display_title" not in {column[1] for column in columns}


async def test_metadata_reservations_catalog_and_operator_audit(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    first_pending = await queue.enqueue(new_job(70))
    first = await queue.claim_next("worker-a")
    assert first is not None and first.id == first_pending.id
    first = await queue.update_metadata(
        first.id,
        "worker-a",
        display_title="  readable   title  " + "界" * 200,
        original_file_name="original.mp4",
        mime_type="video/mp4",
        total_bytes=1234,
    )
    first = await queue.reserve_name(first.id, "worker-a", "clip.mp4")

    second_pending = await queue.enqueue(new_job(71))
    second = await queue.claim_next("worker-b")
    assert second is not None and second.id == second_pending.id
    second = await queue.reserve_name(second.id, "worker-b", "clip.mp4")

    assert first.reserved_name == "clip.mp4"
    assert second.reserved_name == f"clip--{second.short_id}.mp4"
    assert first.display_title is not None
    assert len(first.display_title) == 160
    assert "  " not in first.display_title

    output = tmp_path / "downloads/inbox/clip.mp4"
    await queue.complete(
        first.id,
        "worker-a",
        output,
        relative_path=Path("inbox/clip.mp4"),
    )
    stored = await queue.get_file(first.id)
    assert stored is not None
    assert stored.original_name == "original.mp4"
    assert stored.current_name == "clip.mp4"
    assert stored.display_title == first.display_title
    assert stored.mime_type == "video/mp4"
    assert stored.size_bytes == 1234
    assert stored.location is FileLocation.INBOX

    moved = await queue.record_file_move(
        first.id,
        2002,
        Path("archive/videos/renamed.mp4"),
        tmp_path / "downloads/archive/videos/renamed.mp4",
        action="rename-and-archive",
    )
    stats = await queue.library_stats()
    assert moved.location is FileLocation.ARCHIVE
    assert moved.last_operator_id == 2002
    assert stats.file_count == 1
    assert stats.total_bytes == 1234
    assert stats.inbox_count == 0
    assert stats.archive_count == 1

    async with aiosqlite.connect(queue.path) as connection:
        audit = await (
            await connection.execute(
                """
                SELECT operator_id, action, source_path, destination_path
                FROM file_audit WHERE job_id = ?
                """,
                (str(first.id),),
            )
        ).fetchone()
    assert audit == (
        2002,
        "rename-and-archive",
        "inbox/clip.mp4",
        "archive/videos/renamed.mp4",
    )


async def test_interactions_persist_and_enforce_actor_and_expiry(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    now = datetime.now(UTC)
    active = await queue.create_interaction(
        2002,
        "archive",
        "choose-directory",
        expires_at=now + timedelta(minutes=10),
        pending_name="clip.mp4",
    )

    assert await queue.get_interaction(active.id) == active
    with pytest.raises(KeyError):
        await queue.update_interaction(active.id, 9999, step="confirm")
    updated = await queue.update_interaction(
        active.id,
        2002,
        step="confirm",
        pending_name="renamed.mp4",
        pending_directory="videos",
    )
    assert updated.step == "confirm"
    assert updated.pending_name == "renamed.mp4"
    assert updated.pending_directory == "videos"
    assert not await queue.delete_interaction(active.id, 9999)
    assert await queue.delete_interaction(active.id, 2002)
    assert await queue.get_interaction(active.id) is None

    expired = await queue.create_interaction(
        2002,
        "rename",
        "await-name",
        expires_at=now - timedelta(seconds=1),
    )
    with pytest.raises(KeyError):
        await queue.update_interaction(expired.id, 2002, step="confirm")
