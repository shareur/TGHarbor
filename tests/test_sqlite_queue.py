from __future__ import annotations

import asyncio
import errno
import sqlite3
import traceback
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import aiosqlite
import pytest

from tgharbor.core.errors import (
    PERMANENT_PERSISTENCE_CATEGORIES,
    PERSISTENCE_DETAILS,
    TRANSIENT_PERSISTENCE_CATEGORIES,
    PersistenceError,
)
from tgharbor.core.jobs import (
    BatchNotice,
    DownloadJob,
    FileLocation,
    JobStatus,
    NewDownloadJob,
    SourceSession,
)
from tgharbor.persistence import InvalidJobTransition, SQLiteJobQueue
from tgharbor.persistence.sqlite_queue import _translate_persistence_error


def new_job(
    message_id: int,
    *,
    max_attempts: int = 3,
    requester_id: int = 1001,
    batch_id: str | None = None,
    batch_expected_size: int = 1,
    origin_group_id: str | None = None,
    channel_id: str | None = None,
    created_at: datetime | None = None,
) -> NewDownloadJob:
    return NewDownloadJob(
        requester_id=requester_id,
        source_session=SourceSession.BOT,
        source_chat=str(requester_id),
        source_message_id=message_id,
        max_attempts=max_attempts,
        batch_id=batch_id,
        batch_expected_size=batch_expected_size,
        origin_group_id=origin_group_id,
        channel_id=channel_id,
        created_at=created_at or datetime.now(UTC),
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


async def create_v2_database(path: Path, request: NewDownloadJob) -> None:
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
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                display_title TEXT,
                original_file_name TEXT,
                mime_type TEXT,
                reserved_name TEXT
            );
            CREATE INDEX jobs_claimable_idx
                ON jobs(status, available_at, created_at, id);
            CREATE INDEX jobs_requester_idx
                ON jobs(requester_id, created_at DESC);
            CREATE UNIQUE INDEX jobs_reserved_name_idx
                ON jobs(reserved_name) WHERE reserved_name IS NOT NULL;
            CREATE TABLE stored_files (
                job_id TEXT PRIMARY KEY REFERENCES jobs(id),
                requester_id INTEGER NOT NULL,
                relative_path TEXT NOT NULL UNIQUE,
                original_name TEXT NOT NULL,
                current_name TEXT NOT NULL,
                display_title TEXT,
                mime_type TEXT,
                size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
                location TEXT NOT NULL CHECK(location IN ('inbox', 'archive')),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_operator_id INTEGER
            );
            CREATE INDEX stored_files_updated_idx
                ON stored_files(updated_at DESC, job_id);
            CREATE INDEX stored_files_location_idx
                ON stored_files(location, current_name, job_id);
            CREATE TABLE file_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT REFERENCES stored_files(job_id),
                operator_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                source_path TEXT,
                destination_path TEXT,
                created_at REAL NOT NULL
            );
            CREATE INDEX file_audit_job_idx ON file_audit(job_id, created_at DESC);
            CREATE TABLE interactions (
                id TEXT PRIMARY KEY,
                actor_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                step TEXT NOT NULL,
                job_id TEXT REFERENCES stored_files(job_id),
                pending_name TEXT,
                pending_directory TEXT,
                expires_at REAL NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX interactions_actor_idx
                ON interactions(actor_id, expires_at, updated_at DESC);
            PRAGMA user_version = 2;
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
        await connection.execute(
            """
            INSERT INTO interactions (
                id, actor_id, action, step, job_id, pending_name,
                pending_directory, expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?)
            """,
            (
                "00000000-0000-0000-0000-000000000001",
                1001,
                "rename",
                "ready",
                timestamp + 600,
                timestamp,
                timestamp,
            ),
        )
        await connection.commit()


async def create_v3_database(path: Path, request: NewDownloadJob) -> None:
    await create_v2_database(path, request)
    async with aiosqlite.connect(path) as connection:
        await connection.executescript(
            """
            ALTER TABLE jobs ADD COLUMN batch_id TEXT;
            ALTER TABLE jobs ADD COLUMN batch_expected_size INTEGER NOT NULL DEFAULT 1;
            ALTER TABLE jobs ADD COLUMN started_at REAL;
            ALTER TABLE jobs ADD COLUMN finished_at REAL;
            ALTER TABLE jobs ADD COLUMN resume_required INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE jobs ADD COLUMN origin_group_id TEXT;
            ALTER TABLE jobs ADD COLUMN channel_id TEXT;
            CREATE INDEX jobs_resume_idx
                ON jobs(requester_id, created_at, id) WHERE resume_required = 1;
            CREATE INDEX jobs_batch_idx
                ON jobs(batch_id, status, id) WHERE batch_id IS NOT NULL;
            ALTER TABLE interactions ADD COLUMN batch_id TEXT;
            CREATE TABLE batch_notifications (
                requester_id INTEGER NOT NULL,
                batch_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('pending', 'claimed', 'sent')),
                claimed_by TEXT,
                claimed_at REAL,
                sent_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (requester_id, batch_id)
            );
            CREATE INDEX batch_notifications_state_idx
                ON batch_notifications(state, claimed_at, requester_id, batch_id);
            PRAGMA user_version = 3;
            """
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


async def test_restart_recovery_pauses_or_exhausts_running_jobs(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    await queue.enqueue(new_job(50, max_attempts=2))
    recoverable = await queue.claim_next("worker-a")
    assert recoverable is not None

    await queue.enqueue(new_job(51, max_attempts=1))
    exhausted = await queue.claim_next("worker-b")
    assert exhausted is not None
    ordinary = await queue.enqueue(new_job(52))

    recovered_count, exhausted_count = await queue.recover_interrupted(now=1000)

    assert (recovered_count, exhausted_count) == (1, 1)
    paused = await queue.get(recoverable.id)
    failed = await queue.get(exhausted.id)
    assert paused is not None and paused.status is JobStatus.PENDING
    assert paused.resume_required
    assert paused.claimed_by is None
    assert paused.started_at is None
    assert paused.finished_at is None
    assert paused.attempt_count == 1
    assert failed is not None and failed.status is JobStatus.FAILED
    assert not failed.resume_required
    assert failed.finished_at is not None

    ordinary_now = ordinary.created_at.timestamp() + 1
    claimed_ordinary = await queue.claim_next("worker-c", now=ordinary_now)
    assert claimed_ordinary is not None and claimed_ordinary.id == ordinary.id
    assert await queue.claim_next("worker-c", now=ordinary_now) is None

    assert await queue.list_paused_for_requester(1001) == [paused]
    assert await queue.continue_paused_for_requester(9999) == 0
    assert await queue.continue_paused_for_requester(1001, [exhausted.id]) == 0
    assert await queue.continue_paused_for_requester(1001, now=1002) == 1
    assert await queue.continue_paused_for_requester(1001, now=1003) == 0
    resumed = await queue.claim_next("worker-d", now=1004)
    assert resumed is not None and resumed.id == recoverable.id
    assert resumed.attempt_count == 2
    assert resumed.started_at is not None


async def test_repeated_restart_keeps_paused_jobs_paused(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    await queue.enqueue(new_job(53))
    running = await queue.claim_next("worker")
    assert running is not None

    assert await queue.recover_interrupted(now=1000) == (1, 0)
    assert await queue.recover_interrupted(now=2000) == (0, 0)
    paused = await queue.get(running.id)
    assert paused is not None and paused.status is JobStatus.PENDING
    assert paused.resume_required
    assert await queue.claim_next("worker", now=2000) is None
    assert await queue.continue_paused_for_requester(1001, now=2001) == 1
    resumed = await queue.claim_next("worker", now=2002)
    assert resumed is not None and resumed.id == running.id


async def test_restart_recovery_preserves_a_running_cancellation(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    await queue.enqueue(new_job(54))
    running = await queue.claim_next("worker")
    assert running is not None
    await queue.request_cancel(running.id, 1001)

    recovered_count, exhausted_count = await queue.recover_interrupted(now=1000)

    cancelled = await queue.get(running.id)
    assert (recovered_count, exhausted_count) == (0, 0)
    assert cancelled is not None and cancelled.status is JobStatus.CANCELLED
    assert not cancelled.resume_required
    assert cancelled.finished_at is not None


async def test_schema_newer_than_application_is_rejected(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)

    async with aiosqlite.connect(queue.path) as connection:
        await connection.execute("PRAGMA user_version = 5")
        await connection.commit()

    with pytest.raises(RuntimeError, match="newer than supported"):
        await queue.initialize()


async def test_existing_schema_v4_database_opens_without_running_migrations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await initialized_queue(tmp_path)
    request = await seeded.enqueue(new_job(70))

    async def _unexpected_migration(*args: object, **kwargs: object) -> None:
        raise AssertionError("migration must not run for an existing schema-4 database")

    monkeypatch.setattr(SQLiteJobQueue, "_apply_schema", _unexpected_migration)
    queue = SQLiteJobQueue(seeded.path)
    await queue.initialize()

    reopened = await queue.get(request.id)
    assert reopened is not None and reopened.status is JobStatus.PENDING
    claimed = await queue.claim_next("worker", now=request.created_at.timestamp() + 1)
    assert claimed is not None and claimed.id == request.id


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
    assert migrated.batch_id is None
    assert migrated.batch_expected_size == 1
    assert migrated.started_at is None
    assert migrated.finished_at is None
    assert not migrated.resume_required
    assert migrated.origin_group_id is None
    assert migrated.channel_id is None
    await queue.record_directory_creation(2002, Path("archive/videos"))

    async with aiosqlite.connect(database) as connection:
        version = await (await connection.execute("PRAGMA user_version")).fetchone()
        columns = await (await connection.execute("PRAGMA table_info(jobs)")).fetchall()
        interaction_columns = await (
            await connection.execute("PRAGMA table_info(interactions)")
        ).fetchall()
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

    assert version == (4,)
    assert {column[1] for column in columns} >= {
        "display_title",
        "original_file_name",
        "mime_type",
        "reserved_name",
        "batch_id",
        "batch_expected_size",
        "started_at",
        "finished_at",
        "resume_required",
        "origin_group_id",
        "channel_id",
    }
    assert "prompt_message_id" in {column[1] for column in interaction_columns}
    assert {table[0] for table in tables} >= {
        "stored_files",
        "file_audit",
        "interactions",
        "batch_notifications",
    }
    assert audit == (None, 2002, "mkdir", "archive/videos")


async def test_failed_v1_migration_rolls_back_schema_changes(tmp_path: Path) -> None:
    request = new_job(61)
    database = tmp_path / "state/tgharbor.db"
    await create_v1_database(database, request)
    async with aiosqlite.connect(database) as connection:
        await connection.execute("CREATE TABLE stored_files (sentinel TEXT)")
        await connection.commit()

    with pytest.raises(PersistenceError) as captured:
        await SQLiteJobQueue(database).initialize()

    error = captured.value
    assert error.category == "schema"
    assert error.error_type == "OperationalError"
    assert error.__cause__ is None
    rendered = "".join(traceback.format_exception(error))
    assert str(database) not in rendered
    assert "already exists" not in rendered

    async with aiosqlite.connect(database) as connection:
        version = await (await connection.execute("PRAGMA user_version")).fetchone()
        columns = await (await connection.execute("PRAGMA table_info(jobs)")).fetchall()

    assert version == (1,)
    assert "display_title" not in {column[1] for column in columns}


async def test_clean_v4_initialization_creates_batch_and_prompt_schema(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)

    async with aiosqlite.connect(queue.path) as connection:
        version = await (await connection.execute("PRAGMA user_version")).fetchone()
        job_columns = await (await connection.execute("PRAGMA table_info(jobs)")).fetchall()
        interaction_columns = await (
            await connection.execute("PRAGMA table_info(interactions)")
        ).fetchall()
        tables = await (
            await connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ).fetchall()
        indexes = await (
            await connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        ).fetchall()

    assert version == (4,)
    assert {column[1] for column in job_columns} >= {
        "batch_id",
        "batch_expected_size",
        "started_at",
        "finished_at",
        "resume_required",
        "origin_group_id",
        "channel_id",
    }
    assert {"batch_id", "prompt_message_id"} <= {column[1] for column in interaction_columns}
    assert "batch_notifications" in {table[0] for table in tables}
    assert {index[0] for index in indexes} >= {
        "jobs_resume_idx",
        "jobs_batch_idx",
        "batch_notifications_state_idx",
    }


async def test_v2_database_migrates_to_v4_with_existing_row_defaults(tmp_path: Path) -> None:
    request = new_job(62)
    database = tmp_path / "state/tgharbor.db"
    await create_v2_database(database, request)

    queue = SQLiteJobQueue(database)
    await queue.initialize()

    migrated = await queue.get(request.id)
    assert migrated is not None
    assert migrated.status is JobStatus.PENDING
    assert migrated.batch_id is None
    assert migrated.batch_expected_size == 1
    assert migrated.started_at is None
    assert migrated.finished_at is None
    assert not migrated.resume_required
    assert migrated.origin_group_id is None
    assert migrated.channel_id is None

    async with aiosqlite.connect(database) as connection:
        version = await (await connection.execute("PRAGMA user_version")).fetchone()
        columns = await (await connection.execute("PRAGMA table_info(jobs)")).fetchall()
        interaction = await (
            await connection.execute("SELECT batch_id, prompt_message_id FROM interactions")
        ).fetchone()

    assert version == (4,)
    assert "batch_id" in {column[1] for column in columns}
    assert interaction == (None, None)


@pytest.mark.parametrize(
    ("create", "message_id", "expected_steps"),
    [(create_v1_database, 71, 3), (create_v2_database, 72, 2)],
    ids=["v1", "v2"],
)
async def test_migration_chain_applies_one_transaction_per_version_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    create: Callable[[Path, NewDownloadJob], Awaitable[None]],
    message_id: int,
    expected_steps: int,
) -> None:
    request = new_job(message_id)
    database = tmp_path / "state/tgharbor.db"
    await create(database, request)

    applied: list[str] = []
    original = SQLiteJobQueue._apply_schema

    async def record_apply(connection: aiosqlite.Connection, script: str) -> None:
        applied.append(script)
        await original(connection, script)

    monkeypatch.setattr(SQLiteJobQueue, "_apply_schema", staticmethod(record_apply))
    await SQLiteJobQueue(database).initialize()

    assert len(applied) == expected_steps
    assert all("BEGIN IMMEDIATE" in script for script in applied)
    async with aiosqlite.connect(database) as connection:
        version = await (await connection.execute("PRAGMA user_version")).fetchone()
    assert version == (4,)


async def test_failed_v2_to_v3_migration_rolls_back_schema_changes(tmp_path: Path) -> None:
    request = new_job(63)
    database = tmp_path / "state/tgharbor.db"
    await create_v2_database(database, request)
    async with aiosqlite.connect(database) as connection:
        await connection.execute("CREATE TABLE batch_notifications (sentinel TEXT)")
        await connection.commit()

    with pytest.raises(PersistenceError) as captured:
        await SQLiteJobQueue(database).initialize()

    error = captured.value
    assert error.category == "schema"
    assert error.error_type == "OperationalError"
    assert error.__cause__ is None
    rendered = "".join(traceback.format_exception(error))
    assert str(database) not in rendered
    assert "already exists" not in rendered

    async with aiosqlite.connect(database) as connection:
        version = await (await connection.execute("PRAGMA user_version")).fetchone()
        job_columns = await (await connection.execute("PRAGMA table_info(jobs)")).fetchall()
        interaction_columns = await (
            await connection.execute("PRAGMA table_info(interactions)")
        ).fetchall()

    assert version == (2,)
    assert "batch_id" not in {column[1] for column in job_columns}
    assert "batch_id" not in {column[1] for column in interaction_columns}


async def test_v3_database_migrates_to_v4_preserving_interactions(tmp_path: Path) -> None:
    request = new_job(64)
    database = tmp_path / "state/tgharbor.db"
    await create_v3_database(database, request)

    queue = SQLiteJobQueue(database)
    await queue.initialize()

    migrated = await queue.get(request.id)
    assert migrated is not None and migrated.status is JobStatus.PENDING
    interaction = await queue.get_interaction(UUID("00000000-0000-0000-0000-000000000001"))
    assert interaction is not None
    assert interaction.action == "rename"
    assert interaction.step == "ready"
    assert interaction.prompt_message_id is None

    async with aiosqlite.connect(database) as connection:
        version = await (await connection.execute("PRAGMA user_version")).fetchone()
        columns = await (await connection.execute("PRAGMA table_info(interactions)")).fetchall()

    assert version == (4,)
    assert "prompt_message_id" in {column[1] for column in columns}


async def test_failed_v3_to_v4_migration_rolls_back_schema_changes(tmp_path: Path) -> None:
    request = new_job(65)
    database = tmp_path / "state/tgharbor.db"
    await create_v3_database(database, request)
    async with aiosqlite.connect(database) as connection:
        await connection.execute("ALTER TABLE interactions ADD COLUMN prompt_message_id INTEGER")
        await connection.commit()

    with pytest.raises(PersistenceError) as captured:
        await SQLiteJobQueue(database).initialize()

    error = captured.value
    assert error.category == "schema"
    assert error.error_type == "OperationalError"
    assert error.__cause__ is None
    rendered = "".join(traceback.format_exception(error))
    assert str(database) not in rendered
    assert "duplicate column name" not in rendered

    async with aiosqlite.connect(database) as connection:
        version = await (await connection.execute("PRAGMA user_version")).fetchone()
        interaction = await (
            await connection.execute("SELECT action, step, prompt_message_id FROM interactions")
        ).fetchone()

    assert version == (3,)
    assert interaction == ("rename", "ready", None)


async def test_interaction_prompt_anchor_cas_is_requester_scoped(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    expires_at = datetime.now(UTC) + timedelta(minutes=5)
    session = await queue.create_interaction(
        1001,
        "rename",
        "await-name",
        expires_at=expires_at,
        prompt_message_id=555,
    )

    assert session.prompt_message_id == 555
    assert (await queue.get_interaction(session.id)).prompt_message_id == 555  # type: ignore[union-attr]

    updated = await queue.update_interaction(
        session.id,
        1001,
        step="confirm",
        prompt_message_id=777,
    )
    assert updated.prompt_message_id == 777

    retained = await queue.update_interaction(session.id, 1001, step="await-name")
    assert retained.prompt_message_id == 777

    assert (
        await queue.compare_and_set_prompt_message_id(
            session.id,
            2002,
            expected_prompt_message_id=777,
            prompt_message_id=901,
        )
        is None
    )
    assert (await queue.get_interaction(session.id)).prompt_message_id == 777  # type: ignore[union-attr]

    rebound = await queue.compare_and_set_prompt_message_id(
        session.id,
        1001,
        expected_prompt_message_id=777,
        prompt_message_id=901,
    )
    assert rebound is not None and rebound.prompt_message_id == 901
    assert (
        await queue.compare_and_set_prompt_message_id(
            session.id,
            1001,
            expected_prompt_message_id=777,
            prompt_message_id=902,
        )
        is None
    )
    assert (await queue.get_interaction(session.id)).prompt_message_id == 901  # type: ignore[union-attr]

    with pytest.raises(ValueError):
        await queue.compare_and_set_prompt_message_id(
            session.id,
            1001,
            expected_prompt_message_id=901,
            prompt_message_id=0,
        )
    with pytest.raises(ValueError):
        await queue.create_interaction(
            1001,
            "rename",
            "await-name",
            expires_at=expires_at,
            prompt_message_id=0,
        )

    expired = await queue.create_interaction(
        1001,
        "rename",
        "await-name",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
        prompt_message_id=123,
    )
    assert (
        await queue.compare_and_set_prompt_message_id(
            expired.id,
            1001,
            expected_prompt_message_id=123,
            prompt_message_id=124,
        )
        is None
    )


async def test_concurrent_prompt_anchor_cas_has_one_winner(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    expires_at = datetime.now(UTC) + timedelta(minutes=5)
    session = await queue.create_interaction(1001, "rename", "await-name", expires_at=expires_at)

    async def bind(prompt_message_id: int):
        return await queue.compare_and_set_prompt_message_id(
            session.id,
            1001,
            expected_prompt_message_id=None,
            prompt_message_id=prompt_message_id,
        )

    results = await asyncio.gather(bind(801), bind(802), bind(803))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    stored = await queue.get_interaction(session.id)
    assert stored is not None
    assert stored.prompt_message_id == winners[0].prompt_message_id
    assert stored.prompt_message_id in {801, 802, 803}


async def test_concurrent_text_activation_leaves_exactly_one_live_workflow(
    tmp_path: Path,
) -> None:
    queue = await initialized_queue(tmp_path)
    expiry = datetime.now(UTC) + timedelta(minutes=5)
    first = await queue.create_interaction(1001, "rename", "ready", expires_at=expiry)
    second = await queue.create_interaction(1001, "archive", "await-directory", expires_at=expiry)
    page = await queue.create_interaction(1001, "files-page", "2", expires_at=expiry)
    ready = await queue.create_interaction(1001, "keep", "ready", expires_at=expiry)

    results = await asyncio.gather(
        queue.activate_text_workflow(
            first.id,
            1001,
            action="rename",
            expected_steps=("ready",),
            step="await-name",
        ),
        queue.activate_text_workflow(
            second.id,
            1001,
            action="archive",
            expected_steps=("await-directory",),
            step="confirm",
            pending_directory="videos",
        ),
    )

    assert any(result is not None for result in results)
    async with aiosqlite.connect(queue.path) as connection:
        active = await (
            await connection.execute(
                """
                SELECT id FROM interactions
                WHERE actor_id = 1001
                  AND action IN ('rename', 'archive', 'rename-and-archive', 'batch-move')
                  AND step IN (
                      'await-name', 'await-directory', 'await-new-directory',
                      'confirm', 'confirm-new-directory'
                  )
                """
            )
        ).fetchall()
    assert len(active) == 1
    assert await queue.get_interaction(page.id) is not None
    assert await queue.get_interaction(ready.id) is not None


async def test_concurrent_confirmation_claim_has_one_winner(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    expires_at = datetime.now(UTC) + timedelta(minutes=5)
    session = await queue.create_interaction(
        1001,
        "archive",
        "confirm",
        expires_at=expires_at,
        pending_directory="videos",
    )

    async def claim():
        return await queue.claim_working_interaction(
            session.id,
            1001,
            action="archive",
            expected_steps=("confirm", "confirm-new-directory"),
        )

    results = await asyncio.gather(claim(), claim())
    assert sum(result is not None for result in results) == 1
    stored = await queue.get_interaction(session.id)
    assert stored is not None and stored.step == "working-confirm"
    losers = [result for result in results if result is None]
    assert len(losers) == 1


async def test_page_interaction_consume_is_exact_and_single_use(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    expires_at = datetime.now(UTC) + timedelta(minutes=5)
    session = await queue.create_interaction(
        1001,
        "files-page",
        "2",
        expires_at=expires_at,
        prompt_message_id=555,
    )

    assert (
        await queue.consume_page_interaction(session.id, 2002, action="files-page", step="2")
        is None
    )
    assert (
        await queue.consume_page_interaction(session.id, 1001, action="history-page", step="2")
        is None
    )
    assert (
        await queue.consume_page_interaction(session.id, 1001, action="files-page", step="3")
        is None
    )
    assert await queue.get_interaction(session.id) is not None

    consumed = await queue.consume_page_interaction(session.id, 1001, action="files-page", step="2")
    assert consumed is not None
    assert consumed.id == session.id
    assert consumed.prompt_message_id == 555
    assert await queue.get_interaction(session.id) is None
    assert (
        await queue.consume_page_interaction(session.id, 1001, action="files-page", step="2")
        is None
    )

    expired = await queue.create_interaction(
        1001,
        "files-page",
        "2",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    assert (
        await queue.consume_page_interaction(expired.id, 1001, action="files-page", step="2")
        is None
    )
    assert await queue.get_interaction(expired.id) is not None


async def test_concurrent_page_consume_has_one_winner(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    session = await queue.create_interaction(
        1001,
        "files-page",
        "2",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    async def consume():
        return await queue.consume_page_interaction(session.id, 1001, action="files-page", step="2")

    results = await asyncio.gather(consume(), consume())
    assert sum(result is not None for result in results) == 1
    assert await queue.get_interaction(session.id) is None


async def test_page_restore_is_exact_and_expiry_safe(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    session = await queue.create_interaction(
        1001,
        "files-page",
        "2",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        prompt_message_id=777,
        pending_name="name",
        pending_directory="videos",
    )
    consumed = await queue.consume_page_interaction(session.id, 1001, action="files-page", step="2")
    assert consumed is not None

    assert await queue.restore_page_interaction(consumed) is True
    restored = await queue.get_interaction(session.id)
    assert restored is not None
    assert restored.action == session.action
    assert restored.step == session.step
    assert restored.prompt_message_id == session.prompt_message_id
    assert restored.pending_name == session.pending_name
    assert restored.pending_directory == session.pending_directory
    assert restored.expires_at == session.expires_at
    assert restored.created_at == session.created_at
    assert await queue.restore_page_interaction(consumed) is False

    consumed_again = await queue.consume_page_interaction(
        session.id, 1001, action="files-page", step="2"
    )
    assert consumed_again is not None
    expired_now = session.expires_at.timestamp() + 1
    assert await queue.restore_page_interaction(consumed_again, now=expired_now) is False
    assert await queue.get_interaction(session.id) is None


async def test_recover_working_interactions_restores_explicit_prior_steps(
    tmp_path: Path,
) -> None:
    queue = await initialized_queue(tmp_path)
    now = datetime.now(UTC).timestamp()
    expires_at = datetime.now(UTC) + timedelta(minutes=5)
    renewed_expiry = datetime.now(UTC) + timedelta(hours=1)
    confirm = await queue.create_interaction(
        1001,
        "archive",
        "working-confirm",
        expires_at=expires_at,
        prompt_message_id=111,
        pending_directory="videos",
    )
    new_directory = await queue.create_interaction(
        1001,
        "batch-move",
        "working-confirm-new-directory",
        expires_at=expires_at,
        prompt_message_id=222,
        pending_directory="videos/new",
    )
    legacy = await queue.create_interaction(
        1001,
        "batch-move",
        "working",
        expires_at=expires_at,
        prompt_message_id=333,
        pending_directory="videos",
    )
    expired = await queue.create_interaction(
        1001,
        "archive",
        "working-confirm",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
        prompt_message_id=444,
    )

    recovered_sessions = await queue.recover_working_interactions(
        expires_at=renewed_expiry,
        now=now,
    )
    assert len(recovered_sessions) == 4
    recovered_by_id = {session.id: session for session in recovered_sessions}
    assert recovered_by_id[confirm.id].step == "confirm"
    assert recovered_by_id[confirm.id].prompt_message_id == 111
    assert recovered_by_id[confirm.id].pending_directory == "videos"
    assert recovered_by_id[new_directory.id].step == "confirm-new-directory"
    assert recovered_by_id[new_directory.id].prompt_message_id == 222
    assert recovered_by_id[legacy.id].step == "ready"
    assert recovered_by_id[legacy.id].pending_directory is None
    assert recovered_by_id[expired.id].step == "confirm"
    assert recovered_by_id[expired.id].prompt_message_id == 444
    for recovered in recovered_sessions:
        assert recovered.expires_at == renewed_expiry

    restored = await queue.get_interaction(confirm.id)
    assert restored is not None and restored.step == "confirm"
    assert restored.prompt_message_id == 111
    assert restored.pending_directory == "videos"
    assert restored.expires_at == renewed_expiry

    restored_new = await queue.get_interaction(new_directory.id)
    assert restored_new is not None and restored_new.step == "confirm-new-directory"
    assert restored_new.prompt_message_id == 222
    assert restored_new.pending_directory == "videos/new"
    assert restored_new.expires_at == renewed_expiry

    restored_legacy = await queue.get_interaction(legacy.id)
    assert restored_legacy is not None and restored_legacy.step == "ready"
    assert restored_legacy.prompt_message_id == 333
    assert restored_legacy.pending_directory is None
    assert restored_legacy.expires_at == renewed_expiry

    recovered_expired = await queue.get_interaction(expired.id)
    assert recovered_expired is not None and recovered_expired.step == "confirm"
    assert recovered_expired.expires_at == renewed_expiry


async def test_restore_expired_working_interaction_renews_actionable_expiry(
    tmp_path: Path,
) -> None:
    queue = await initialized_queue(tmp_path)
    expired = await queue.create_interaction(
        1001,
        "archive",
        "working-confirm-new-directory",
        expires_at=datetime.now(UTC) - timedelta(hours=2),
        prompt_message_id=451,
        pending_name="clip.mp4",
        pending_directory="videos/new",
    )
    renewed_expiry = datetime.now(UTC) + timedelta(hours=1)

    restored = await queue.restore_working_interaction(
        expired.id,
        1001,
        step="confirm-new-directory",
        prompt_message_id=451,
        expires_at=renewed_expiry,
        pending_name="clip.mp4",
        pending_directory="videos/new",
    )

    assert restored is not None
    assert restored.step == "confirm-new-directory"
    assert restored.prompt_message_id == 451
    assert restored.pending_directory == "videos/new"
    assert restored.expires_at == renewed_expiry
    # The renewed row is now actionable for its owner.
    assert (
        await queue.claim_working_interaction(
            restored.id,
            1001,
            action="archive",
            expected_steps=("confirm-new-directory",),
        )
        is not None
    )


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
        origin_group_id="-100123",
        channel_id="-100456",
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
    assert first.origin_group_id == "-100123"
    assert first.channel_id == "-100456"
    assert second.origin_group_id is None
    assert second.channel_id is None

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


async def test_claim_completion_and_retry_timestamps(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    pending = await queue.enqueue(
        new_job(80, max_attempts=2, created_at=datetime.fromtimestamp(990, UTC))
    )
    claimed = await queue.claim_next("worker", now=1000)

    assert claimed is not None and claimed.id == pending.id
    assert claimed.started_at == datetime.fromtimestamp(1000, UTC)
    assert claimed.finished_at is None

    completed = await queue.mark_completed(
        claimed.id,
        "worker",
        Path("/downloads/clip.bin"),
        now=1010,
    )
    assert completed.finished_at == datetime.fromtimestamp(1010, UTC)
    assert completed.started_at == datetime.fromtimestamp(1000, UTC)

    retried_job = await queue.enqueue(
        new_job(81, max_attempts=2, created_at=datetime.fromtimestamp(1990, UTC))
    )
    first = await queue.claim_next("worker", now=2000)
    assert first is not None and first.id == retried_job.id
    retried = await queue.fail_or_retry(
        first.id,
        "worker",
        "temporary",
        retryable=True,
        now=2005,
    )
    assert retried.status is JobStatus.PENDING
    assert retried.started_at is None
    assert retried.finished_at is None

    second = await queue.claim_next("worker", now=2010)
    assert second is not None
    assert second.started_at == datetime.fromtimestamp(2010, UTC)
    failed = await queue.fail_or_retry(
        second.id,
        "worker",
        "permanent",
        retryable=False,
        now=2015,
    )
    assert failed.status is JobStatus.FAILED
    assert failed.finished_at == datetime.fromtimestamp(2015, UTC)
    assert failed.started_at == datetime.fromtimestamp(2010, UTC)


async def enqueue_batch(
    queue: SQLiteJobQueue,
    batch_id: str,
    count: int,
    expected: int,
    *,
    start_message_id: int = 1000,
) -> list[DownloadJob]:
    return [
        await queue.enqueue(
            new_job(
                start_message_id + index,
                batch_id=batch_id,
                batch_expected_size=expected,
            )
        )
        for index in range(count)
    ]


async def complete_next(queue: SQLiteJobQueue, *, worker: str = "worker") -> DownloadJob:
    claimed = await queue.claim_next(worker)
    assert claimed is not None
    return await queue.mark_completed(
        claimed.id,
        worker,
        Path(f"/downloads/{claimed.id.hex}.bin"),
        relative_path=Path(f"inbox/{claimed.id.hex}.bin"),
    )


async def fail_next(queue: SQLiteJobQueue, *, worker: str = "worker") -> DownloadJob:
    claimed = await queue.claim_next(worker)
    assert claimed is not None
    return await queue.fail_or_retry(
        claimed.id,
        worker,
        "permanent test failure",
        retryable=False,
    )


async def test_batch_progress_reports_partial_failure(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    await enqueue_batch(queue, "batch-progress", 2, 2)
    await complete_next(queue)
    await fail_next(queue)

    progress = await queue.batch_progress_for_requester(1001, "batch-progress")
    assert progress is not None
    assert progress.expected_size == 2
    assert progress.total_jobs == 2
    assert progress.completed == 1
    assert progress.failed == 1
    assert not progress.all_succeeded
    assert await queue.batch_progress_for_requester(9999, "batch-progress") is None
    assert await queue.batch_progress_for_requester(1001, "missing") is None


async def test_batch_notification_claim_is_serial_and_idempotent(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    await enqueue_batch(queue, "batch-serial", 2, 2)
    await complete_next(queue)
    await complete_next(queue)

    assert await queue.claim_batch_notification(
        "batch-serial", 1001, claimed_by="sender-a", now=1000
    )
    assert not await queue.claim_batch_notification(
        "batch-serial", 1001, claimed_by="sender-b", now=1001
    )
    assert await queue.mark_batch_notification_sent(
        "batch-serial", 1001, claimed_by="sender-a", now=1002
    )
    assert await queue.mark_batch_notification_sent(
        "batch-serial", 1001, claimed_by="sender-a", now=1003
    )
    assert not await queue.claim_batch_notification(
        "batch-serial", 1001, claimed_by="sender-b", now=1004
    )
    assert not await queue.release_batch_notification(
        "batch-serial", 1001, claimed_by="sender-a", now=1005
    )


async def test_batch_notification_concurrent_claim_has_one_winner(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    await enqueue_batch(queue, "batch-concurrent", 2, 2)
    await complete_next(queue)
    await complete_next(queue)

    results = await asyncio.gather(
        *(
            queue.claim_batch_notification(
                "batch-concurrent",
                1001,
                claimed_by=f"sender-{index}",
                now=1000,
            )
            for index in range(4)
        )
    )

    assert sum(results) == 1


async def test_batch_notification_release_and_sent_state(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    await enqueue_batch(queue, "batch-release", 2, 2)
    await complete_next(queue)
    await complete_next(queue)

    assert await queue.claim_batch_notification(
        "batch-release", 1001, claimed_by="sender-a", now=1000
    )
    assert not await queue.release_batch_notification(
        "batch-release", 1001, claimed_by="sender-b", now=1001
    )
    assert await queue.release_batch_notification(
        "batch-release", 1001, claimed_by="sender-a", now=1002
    )
    assert await queue.claim_batch_notification(
        "batch-release", 1001, claimed_by="sender-b", now=1003
    )
    assert not await queue.release_batch_notification(
        "batch-release", 1001, claimed_by="sender-a", now=1004
    )
    assert await queue.mark_batch_notification_sent(
        "batch-release", 1001, claimed_by="sender-b", now=1005
    )
    assert not await queue.claim_batch_notification(
        "batch-release", 1001, claimed_by="sender-c", now=1006
    )


async def test_batch_notification_stale_claim_can_be_taken_over(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    await enqueue_batch(queue, "batch-stale", 2, 2)
    await complete_next(queue)
    await complete_next(queue)

    assert await queue.claim_batch_notification(
        "batch-stale", 1001, claimed_by="old", now=1000, stale_after_seconds=60
    )
    assert not await queue.claim_batch_notification(
        "batch-stale", 1001, claimed_by="new", now=1059, stale_after_seconds=60
    )
    assert await queue.claim_batch_notification(
        "batch-stale", 1001, claimed_by="new", now=1060, stale_after_seconds=60
    )
    assert not await queue.mark_batch_notification_sent(
        "batch-stale", 1001, claimed_by="old", now=1061
    )
    assert await queue.mark_batch_notification_sent("batch-stale", 1001, claimed_by="new", now=1062)


async def test_restart_recovery_releases_stale_batch_notice_claims(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    await enqueue_batch(queue, "batch-startup", 2, 2)
    await complete_next(queue)
    await complete_next(queue)
    assert await queue.claim_batch_notification(
        "batch-startup", 1001, claimed_by="crashed", now=1000
    )

    assert await queue.recover_interrupted(now=1059, notice_stale_after_seconds=60) == (0, 0)
    assert not await queue.claim_batch_notification(
        "batch-startup", 1001, claimed_by="replacement", now=1059
    )
    assert await queue.recover_interrupted(now=1060, notice_stale_after_seconds=60) == (0, 0)
    assert await queue.claim_batch_notification(
        "batch-startup", 1001, claimed_by="replacement", now=1061
    )


async def test_recover_stale_batch_notifications_releases_claims(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    await enqueue_batch(queue, "batch-restart", 2, 2)
    await complete_next(queue)
    await complete_next(queue)

    assert await queue.claim_batch_notification(
        "batch-restart", 1001, claimed_by="crashed", now=1000
    )
    assert await queue.recover_stale_batch_notifications(now=1060, stale_after_seconds=60) == 1
    assert await queue.claim_batch_notification(
        "batch-restart", 1001, claimed_by="replacement", now=1061
    )
    assert await queue.recover_stale_batch_notifications(now=1062, stale_after_seconds=60) == 0


async def test_startup_recovery_releases_all_batch_notice_claims(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    await enqueue_batch(queue, "batch-startup-all", 2, 2)
    await complete_next(queue)
    await complete_next(queue)

    assert await queue.claim_batch_notification(
        "batch-startup-all", 1001, claimed_by="crashed", now=1000
    )
    assert await queue.recover_interrupted(now=1000, notice_stale_after_seconds=0) == (0, 0)
    assert await queue.claim_batch_notification(
        "batch-startup-all", 1001, claimed_by="replacement", now=1000
    )


async def test_list_claimable_batch_notifications_is_bounded_and_durable(
    tmp_path: Path,
) -> None:
    queue = await initialized_queue(tmp_path)
    for index, batch_id in enumerate(("batch-a", "batch-b")):
        await enqueue_batch(queue, batch_id, 2, 2, start_message_id=1000 + index * 10)
        await complete_next(queue)
        await complete_next(queue)

    assert await queue.list_claimable_batch_notifications(limit=1, now=1000) == [
        BatchNotice(requester_id=1001, batch_id="batch-a")
    ]
    assert await queue.list_claimable_batch_notifications(limit=20, now=1000) == [
        BatchNotice(requester_id=1001, batch_id="batch-a"),
        BatchNotice(requester_id=1001, batch_id="batch-b"),
    ]

    assert await queue.claim_batch_notification(
        "batch-a", 1001, claimed_by="sender", now=1000, stale_after_seconds=60
    )
    assert await queue.list_claimable_batch_notifications(
        limit=20, now=1059, stale_after_seconds=60
    ) == [BatchNotice(requester_id=1001, batch_id="batch-b")]
    assert await queue.list_claimable_batch_notifications(
        limit=20, now=1060, stale_after_seconds=60
    ) == [
        BatchNotice(requester_id=1001, batch_id="batch-a"),
        BatchNotice(requester_id=1001, batch_id="batch-b"),
    ]

    assert await queue.release_batch_notification("batch-a", 1001, claimed_by="sender", now=1061)
    assert await queue.list_claimable_batch_notifications(
        limit=20, now=1061, stale_after_seconds=60
    ) == [
        BatchNotice(requester_id=1001, batch_id="batch-a"),
        BatchNotice(requester_id=1001, batch_id="batch-b"),
    ]

    assert await queue.claim_batch_notification("batch-a", 1001, claimed_by="sender", now=1062)
    assert await queue.mark_batch_notification_sent("batch-a", 1001, claimed_by="sender", now=1063)
    assert await queue.list_claimable_batch_notifications(
        limit=20, now=1100, stale_after_seconds=60
    ) == [BatchNotice(requester_id=1001, batch_id="batch-b")]

    with pytest.raises(ValueError):
        await queue.list_claimable_batch_notifications(limit=0)
    with pytest.raises(ValueError):
        await queue.list_claimable_batch_notifications(stale_after_seconds=-1)


async def test_list_claimable_batch_notifications_excludes_ineligible_batches(
    tmp_path: Path,
) -> None:
    partial = await initialized_queue(tmp_path / "partial")
    await enqueue_batch(partial, "batch-partial", 2, 2)
    await complete_next(partial)
    assert await partial.list_claimable_batch_notifications(now=1000) == []

    single = await initialized_queue(tmp_path / "single")
    await enqueue_batch(single, "batch-single", 1, 1)
    await complete_next(single)
    assert await single.list_claimable_batch_notifications(now=1000) == []

    failed = await initialized_queue(tmp_path / "failed")
    await enqueue_batch(failed, "batch-failed", 2, 2)
    await complete_next(failed)
    await fail_next(failed)
    assert await failed.list_claimable_batch_notifications(now=1000) == []


async def test_ensure_batch_move_interaction_reuses_one_row(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    expires_at = datetime.now(UTC) + timedelta(hours=1)

    first = await queue.ensure_batch_move_interaction(
        1001,
        "batch-interaction",
        expires_at=expires_at,
        now=1000,
    )
    second = await queue.ensure_batch_move_interaction(
        1001,
        "batch-interaction",
        expires_at=expires_at + timedelta(hours=1),
        now=2000,
    )

    assert second.id == first.id
    assert second.step == "ready"
    async with aiosqlite.connect(queue.path) as connection:
        cursor = await connection.execute(
            "SELECT COUNT(*) FROM interactions WHERE actor_id = ? AND batch_id = ?",
            (1001, "batch-interaction"),
        )
        row = await cursor.fetchone()
    assert row is not None and row[0] == 1
    assert (
        await queue.active_batch_move_interaction(1001, "batch-interaction", now=2000) is not None
    )


async def test_active_batch_move_interaction_is_scoped_and_live(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    session = await queue.create_interaction(
        1001,
        "batch-move",
        "ready",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        batch_id="batch-lookup",
    )

    found = await queue.active_batch_move_interaction(1001, "batch-lookup")
    assert found is not None
    assert found.id == session.id
    assert await queue.active_batch_move_interaction(2002, "batch-lookup") is None
    assert await queue.active_batch_move_interaction(1001, "batch-other") is None

    await queue.delete_interaction(session.id, 1001)
    assert await queue.active_batch_move_interaction(1001, "batch-lookup") is None

    await queue.create_interaction(
        1001,
        "batch-move",
        "ready",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        batch_id="batch-expired-lookup",
    )
    assert await queue.active_batch_move_interaction(1001, "batch-expired-lookup") is None


async def test_incomplete_single_and_failed_batches_are_not_notifiable(
    tmp_path: Path,
) -> None:
    incomplete = await initialized_queue(tmp_path / "incomplete")
    await enqueue_batch(incomplete, "incomplete", 2, 2)
    await complete_next(incomplete)
    assert not await incomplete.claim_batch_notification("incomplete", 1001, claimed_by="sender")

    single = await initialized_queue(tmp_path / "single")
    await enqueue_batch(single, "single", 1, 1)
    await complete_next(single)
    assert not await single.claim_batch_notification("single", 1001, claimed_by="sender")

    failed = await initialized_queue(tmp_path / "failed")
    await enqueue_batch(failed, "failed", 2, 2)
    await complete_next(failed)
    await fail_next(failed)
    assert not await failed.claim_batch_notification("failed", 1001, claimed_by="sender")


async def test_activate_text_workflow_keeps_one_and_spares_pages_and_ready(
    tmp_path: Path,
) -> None:
    queue = await initialized_queue(tmp_path)
    expiry = datetime.now(UTC) + timedelta(minutes=5)
    older = await queue.create_interaction(1001, "rename", "await-name", expires_at=expiry)
    page = await queue.create_interaction(1001, "files-page", "2", expires_at=expiry)
    ready = await queue.create_interaction(1001, "keep", "ready", expires_at=expiry)
    newer = await queue.create_interaction(1001, "archive", "ready", expires_at=expiry)
    other_actor = await queue.create_interaction(2002, "rename", "await-name", expires_at=expiry)
    old_confirm = await queue.create_interaction(1001, "batch-move", "confirm", expires_at=expiry)

    activated = await queue.activate_text_workflow(
        newer.id,
        1001,
        action="archive",
        expected_steps=("ready",),
        step="await-directory",
    )

    assert activated is not None and activated.step == "await-directory"
    assert await queue.get_interaction(older.id) is None
    assert await queue.get_interaction(old_confirm.id) is None
    assert await queue.get_interaction(newer.id) is not None
    assert await queue.get_interaction(page.id) is not None
    assert await queue.get_interaction(ready.id) is not None
    assert await queue.get_interaction(other_actor.id) is not None

    assert (
        await queue.activate_text_workflow(
            other_actor.id,
            2002,
            action="rename",
            expected_steps=("await-name",),
            step="confirm",
        )
        is not None
    )
    assert await queue.get_interaction(other_actor.id) is not None


async def stored_job_id(queue: SQLiteJobQueue, message_id: int = 500) -> UUID:
    pending = await queue.enqueue(new_job(message_id))
    running = await queue.claim_next("worker")
    assert running is not None and running.id == pending.id
    await queue.complete(
        running.id,
        "worker",
        Path(f"/downloads/inbox/{message_id}.bin"),
        relative_path=Path(f"inbox/{message_id}.bin"),
    )
    return pending.id


async def test_record_file_move_replay_does_not_duplicate_audit(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    job_id = await stored_job_id(queue)
    destination = Path("archive/videos/500.bin")

    first = await queue.record_file_move(
        job_id,
        1001,
        destination,
        Path("/downloads/archive/videos/500.bin"),
        action="archive",
    )
    replay = await queue.record_file_move(
        job_id,
        2002,
        destination,
        Path("/downloads/archive/videos/500.bin"),
        action="archive",
    )

    assert first.relative_path == replay.relative_path == destination
    assert replay.last_operator_id == 2002
    async with aiosqlite.connect(queue.path) as connection:
        rows = await (
            await connection.execute(
                """
                SELECT operator_id, action, source_path, destination_path
                FROM file_audit WHERE job_id = ?
                """,
                (str(job_id),),
            )
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        (1001, "archive", "inbox/500.bin", "archive/videos/500.bin")
    ]


async def test_record_directory_creation_replay_does_not_duplicate_audit(
    tmp_path: Path,
) -> None:
    queue = await initialized_queue(tmp_path)
    directory = Path("archive/videos")

    await queue.record_directory_creation(1001, directory)
    await queue.record_directory_creation(1001, directory)
    await queue.record_directory_creation(2002, directory)

    async with aiosqlite.connect(queue.path) as connection:
        rows = await (
            await connection.execute(
                """
                SELECT operator_id, action, source_path, destination_path
                FROM file_audit WHERE action = 'mkdir'
                """
            )
        ).fetchall()
    assert [tuple(row) for row in rows] == [(1001, "mkdir", None, "archive/videos")]


async def test_result_pending_transition_is_exact_and_deletes_siblings(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    job_id = await stored_job_id(queue)
    expiry = datetime.now(UTC) + timedelta(minutes=5)
    sibling = await queue.create_interaction(
        1001,
        "keep",
        "ready",
        expires_at=expiry,
        job_id=job_id,
        prompt_message_id=10,
    )
    other_job = await stored_job_id(queue, 501)
    unrelated = await queue.create_interaction(
        1001,
        "rename",
        "ready",
        expires_at=expiry,
        job_id=other_job,
        prompt_message_id=11,
    )
    confirmation = await queue.create_interaction(
        1001,
        "archive",
        "confirm",
        expires_at=expiry,
        job_id=job_id,
        pending_directory="videos",
        prompt_message_id=12,
    )
    claimed = await queue.claim_working_interaction(
        confirmation.id,
        1001,
        action="archive",
        expected_steps=("confirm", "confirm-new-directory"),
    )
    assert claimed is not None

    # A different actor, action, or source step cannot consume the claim.
    assert (
        await queue.persist_result_pending(
            confirmation.id,
            2002,
            action="archive",
            expected_steps=("working-confirm",),
            job_id=job_id,
        )
        is None
    )
    assert (
        await queue.persist_result_pending(
            confirmation.id,
            1001,
            action="rename",
            expected_steps=("working-confirm",),
            job_id=job_id,
        )
        is None
    )
    assert (
        await queue.persist_result_pending(
            confirmation.id,
            1001,
            action="archive",
            expected_steps=("working-confirm-new-directory",),
            job_id=job_id,
        )
        is None
    )
    still_working = await queue.get_interaction(confirmation.id)
    assert still_working is not None and still_working.step == "working-confirm"

    pending = await queue.persist_result_pending(
        confirmation.id,
        1001,
        action="archive",
        expected_steps=("working-confirm", "working-confirm-new-directory"),
        job_id=job_id,
    )
    assert pending is not None
    assert pending.step == "result-pending"
    assert pending.prompt_message_id == 12
    assert pending.pending_directory is None
    assert await queue.get_interaction(sibling.id) is None
    assert await queue.get_interaction(unrelated.id) is not None
    # The transition is single-use: the working step predicate no longer matches.
    assert (
        await queue.persist_result_pending(
            confirmation.id,
            1001,
            action="archive",
            expected_steps=("working-confirm",),
            job_id=job_id,
        )
        is None
    )

    async with aiosqlite.connect(queue.path) as connection:
        row = await (await connection.execute("PRAGMA user_version")).fetchone()
    assert row is not None and row[0] == 4


async def test_retry_pending_requires_known_delivery_before_ready(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    expiry = datetime.now(UTC) + timedelta(minutes=5)
    confirmation = await queue.create_interaction(
        1001,
        "batch-move",
        "confirm",
        expires_at=expiry,
        batch_id="batch-retry",
        pending_directory="videos",
        prompt_message_id=333,
    )
    claimed = await queue.claim_working_interaction(
        confirmation.id,
        1001,
        action="batch-move",
        expected_steps=("confirm", "confirm-new-directory"),
    )
    assert claimed is not None
    assert (
        await queue.persist_retry_pending(
            confirmation.id,
            1001,
            action="archive",
            expected_steps=("working-confirm",),
        )
        is None
    )
    pending = await queue.persist_retry_pending(
        confirmation.id,
        1001,
        action="batch-move",
        expected_steps=("working-confirm",),
    )
    assert pending is not None and pending.step == "retry-pending"
    assert pending.pending_directory is None

    renewed_expiry = datetime.now(UTC) + timedelta(hours=1)
    # The anchor must still match the actual delivered card.
    assert (
        await queue.mark_retry_ready(
            confirmation.id,
            1001,
            action="batch-move",
            expected_prompt_message_id=334,
            expires_at=renewed_expiry,
        )
        is None
    )
    assert (
        await queue.bind_pending_prompt_message_id(
            confirmation.id,
            1001,
            expected_step="retry-pending",
            expected_prompt_message_id=333,
            prompt_message_id=334,
        )
        is not None
    )
    ready = await queue.mark_retry_ready(
        confirmation.id,
        1001,
        action="batch-move",
        expected_prompt_message_id=334,
        expires_at=renewed_expiry,
    )
    assert ready is not None and ready.step == "ready"
    assert ready.prompt_message_id == 334
    assert ready.expires_at == renewed_expiry
    assert (
        await queue.mark_retry_ready(
            confirmation.id,
            1001,
            action="batch-move",
            expected_prompt_message_id=334,
            expires_at=renewed_expiry,
        )
        is None
    )


async def test_mark_retry_ready_renews_expired_retry_pending(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    expired = await queue.create_interaction(
        1001,
        "batch-move",
        "retry-pending",
        expires_at=datetime.now(UTC) - timedelta(hours=2),
        batch_id="batch-expired-retry",
        prompt_message_id=470,
    )
    renewed_expiry = datetime.now(UTC) + timedelta(hours=1)

    ready = await queue.mark_retry_ready(
        expired.id,
        1001,
        action="batch-move",
        expected_prompt_message_id=470,
        expires_at=renewed_expiry,
    )

    assert ready is not None
    assert ready.step == "ready"
    assert ready.prompt_message_id == 470
    assert ready.expires_at == renewed_expiry
    assert await queue.latest_interaction_for_actor(1001) is None
    # The ready row is actionable for its owner and rejectable for another actor.
    assert (
        await queue.activate_text_workflow(
            expired.id,
            1001,
            action="batch-move",
            expected_steps=("ready",),
            step="await-directory",
        )
        is not None
    )


async def test_pending_delivery_rows_ignore_actionable_expiry(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    expired = datetime.now(UTC) - timedelta(minutes=1)
    result = await queue.create_interaction(
        1001,
        "archive",
        "result-pending",
        expires_at=expired,
        pending_directory=None,
        prompt_message_id=440,
    )
    retry = await queue.create_interaction(
        1001,
        "batch-move",
        "retry-pending",
        expires_at=expired,
        batch_id="batch-expired",
        prompt_message_id=441,
    )
    # Non-working actionable rows keep their stale expiry semantics.
    stale = await queue.create_interaction(
        1001,
        "archive",
        "confirm",
        expires_at=expired,
        prompt_message_id=442,
    )
    assert (
        await queue.claim_working_interaction(
            stale.id,
            1001,
            action="archive",
            expected_steps=("confirm",),
        )
        is None
    )
    assert await queue.latest_interaction_for_actor(1001) is None
    assert (
        await queue.recover_working_interactions(expires_at=datetime.now(UTC) + timedelta(hours=1))
        == []
    )

    listed = await queue.list_pending_delivery_interactions()
    assert {session.id for session in listed} == {result.id, retry.id}
    assert await queue.get_interaction(result.id) is not None
    assert await queue.get_interaction(retry.id) is not None
    with pytest.raises(ValueError):
        await queue.restore_working_interaction(
            result.id,
            1001,
            step="result-pending",
            prompt_message_id=440,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )

    bound = await queue.bind_pending_prompt_message_id(
        result.id,
        1001,
        expected_step="result-pending",
        expected_prompt_message_id=440,
        prompt_message_id=450,
    )
    assert bound is not None and bound.prompt_message_id == 450
    assert await queue.finish_result_delivery(
        result.id,
        1001,
        action="archive",
        expected_step="result-pending",
        expected_prompt_message_id=450,
    )
    assert await queue.get_interaction(result.id) is None
    assert [session.id for session in await queue.list_pending_delivery_interactions()] == [
        retry.id
    ]


async def test_finish_result_delivery_is_single_winner_with_anchor_cas(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    session = await queue.create_interaction(
        1001,
        "rename-and-archive",
        "result-pending",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        prompt_message_id=555,
    )
    assert not await queue.finish_result_delivery(
        session.id,
        2002,
        action="rename-and-archive",
        expected_step="result-pending",
        expected_prompt_message_id=555,
    )
    assert not await queue.finish_result_delivery(
        session.id,
        1001,
        action="archive",
        expected_step="result-pending",
        expected_prompt_message_id=555,
    )
    assert not await queue.finish_result_delivery(
        session.id,
        1001,
        action="rename-and-archive",
        expected_step="retry-pending",
        expected_prompt_message_id=555,
    )
    assert not await queue.finish_result_delivery(
        session.id,
        1001,
        action="rename-and-archive",
        expected_step="result-pending",
        expected_prompt_message_id=556,
    )
    assert await queue.get_interaction(session.id) is not None

    async def settle() -> bool:
        return await queue.finish_result_delivery(
            session.id,
            1001,
            action="rename-and-archive",
            expected_step="result-pending",
            expected_prompt_message_id=555,
        )

    winners = await asyncio.gather(settle(), settle())
    assert sum(winners) == 1
    assert await queue.get_interaction(session.id) is None


async def test_delete_interactions_for_job_retains_undelivered_results(tmp_path: Path) -> None:
    queue = await initialized_queue(tmp_path)
    job_id = await stored_job_id(queue)
    expiry = datetime.now(UTC) + timedelta(minutes=5)
    actionable = await queue.create_interaction(
        1001, "keep", "ready", expires_at=expiry, job_id=job_id
    )
    pending = await queue.create_interaction(
        1001,
        "archive",
        "result-pending",
        expires_at=expiry,
        job_id=job_id,
        prompt_message_id=700,
    )
    retry = await queue.create_interaction(
        1001,
        "batch-move",
        "retry-pending",
        expires_at=expiry,
        job_id=job_id,
        prompt_message_id=701,
    )

    assert await queue.delete_interactions_for_job(job_id) == 1
    assert await queue.get_interaction(actionable.id) is None
    assert await queue.get_interaction(pending.id) is not None
    assert await queue.get_interaction(retry.id) is not None


async def test_default_busy_timeout_is_fifteen_seconds(tmp_path: Path) -> None:
    queue = SQLiteJobQueue(tmp_path / "state/tgharbor.db")

    assert queue._busy_timeout_ms == 15_000
    await queue.initialize()
    async with queue._connect() as connection:
        cursor = await connection.execute("PRAGMA busy_timeout")
        row = await cursor.fetchone()

    assert row is not None
    assert row[0] == 15_000


async def test_contended_write_succeeds_after_lock_release(tmp_path: Path) -> None:
    queue = SQLiteJobQueue(tmp_path / "state/tgharbor.db", busy_timeout_seconds=5)
    await queue.initialize()
    holder = await aiosqlite.connect(queue.path)
    try:
        await holder.execute("BEGIN IMMEDIATE")

        async def release() -> None:
            await asyncio.sleep(0.2)
            await holder.commit()

        release_task = asyncio.create_task(release())
        await asyncio.wait_for(
            queue.record_directory_creation(2002, Path("archive/locked")), timeout=4
        )
        await release_task
    finally:
        await holder.rollback()
        await holder.close()


async def test_short_busy_timeout_raises_sanitized_busy_error(tmp_path: Path) -> None:
    queue = SQLiteJobQueue(tmp_path / "state/tgharbor.db", busy_timeout_seconds=0.01)
    await queue.initialize()
    sentinel = "JOB_SENTINEL_CHAT"
    request = NewDownloadJob(
        requester_id=1001,
        source_session=SourceSession.BOT,
        source_chat=sentinel,
        source_message_id=99,
    )
    holder = await aiosqlite.connect(queue.path)
    try:
        await holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(PersistenceError) as captured:
            await queue.enqueue(request)
    finally:
        await holder.rollback()
        await holder.close()

    error = captured.value
    assert error.category == "busy"
    assert error.code == 5
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    rendered = "".join(traceback.format_exception(error))
    assert str(queue.path) not in rendered
    assert sentinel not in rendered
    assert "database is locked" not in rendered


async def test_open_failure_is_sanitized(tmp_path: Path) -> None:
    database = tmp_path / "state/tgharbor.db"
    database.mkdir(parents=True)

    with pytest.raises(PersistenceError) as captured:
        await SQLiteJobQueue(database).initialize()

    error = captured.value
    assert error.category == "open"
    assert error.code == 14
    rendered = "".join(traceback.format_exception(error))
    assert str(database) not in rendered
    assert "unable to open database file" not in rendered


@pytest.mark.parametrize(
    ("error", "category", "code"),
    [
        (sqlite3.InterfaceError("Error binding parameter 0"), "adapter", None),
        (aiosqlite.OperationalError("database is locked"), "busy", None),
        (aiosqlite.OperationalError("unable to open database file"), "open", None),
        (aiosqlite.OperationalError("disk I/O error"), "io", None),
        (aiosqlite.OperationalError("database or disk is full"), "disk-full", None),
        (aiosqlite.OperationalError("attempt to write a readonly database"), "read-only", None),
        (aiosqlite.DatabaseError("database disk image is malformed"), "corrupt", None),
        (aiosqlite.OperationalError("no such table: jobs"), "schema", None),
        (aiosqlite.IntegrityError("UNIQUE constraint failed"), "integrity", None),
        (OSError(errno.ENOSPC, "No space left on device"), "disk-full", errno.ENOSPC),
        (OSError(errno.EACCES, "Permission denied"), "permission", errno.EACCES),
        (OSError(errno.EROFS, "Read-only file system"), "read-only", errno.EROFS),
        (OSError(errno.EIO, "Input/output error"), "io", errno.EIO),
        (OSError(errno.ENOENT, "No such file"), "open", errno.ENOENT),
    ],
)
def test_translation_categories_are_sanitized(
    error: BaseException, category: str, code: int | None
) -> None:
    translated = _translate_persistence_error(error)

    assert translated.category == category
    assert translated.detail == PERSISTENCE_DETAILS[category]
    assert translated.code == code
    assert translated.__cause__ is None
    assert str(translated) == f"{category}: {PERSISTENCE_DETAILS[category]}"


def test_primary_sqlite_code_takes_precedence_over_message() -> None:
    error = sqlite3.OperationalError("SENSITIVE_MESSAGE_SENTINEL")
    error.sqlite_errorcode = 13

    translated = _translate_persistence_error(error)

    assert (translated.category, translated.code) == ("disk-full", 13)
    assert "SENSITIVE_MESSAGE_SENTINEL" not in str(translated)


def test_already_translated_error_is_preserved() -> None:
    original = PersistenceError(
        "busy",
        PERSISTENCE_DETAILS["busy"],
        error_type="OperationalError",
        code=5,
    )

    assert _translate_persistence_error(original) is original


def test_persistence_error_rejects_non_allowlisted_detail() -> None:
    with pytest.raises(ValueError):
        PersistenceError(
            "busy",
            "database is locked at /private/path",
            error_type="OperationalError",
        )


def test_persistence_category_sets_partition_every_detail() -> None:
    assert TRANSIENT_PERSISTENCE_CATEGORIES | PERMANENT_PERSISTENCE_CATEGORIES == frozenset(
        PERSISTENCE_DETAILS
    )
    assert not TRANSIENT_PERSISTENCE_CATEGORIES & PERMANENT_PERSISTENCE_CATEGORIES
    assert TRANSIENT_PERSISTENCE_CATEGORIES == frozenset(
        {"busy", "io", "disk-full", "permission", "read-only", "open"}
    )
    assert PERMANENT_PERSISTENCE_CATEGORIES == frozenset(
        {"corrupt", "schema", "integrity", "adapter", "database"}
    )


@pytest.mark.parametrize("category", sorted(TRANSIENT_PERSISTENCE_CATEGORIES))
def test_transient_persistence_categories_are_retryable(category: str) -> None:
    error = PersistenceError(
        category,
        PERSISTENCE_DETAILS[category],
        error_type="OperationalError",
    )

    assert error.retryable is True


@pytest.mark.parametrize("category", sorted(PERMANENT_PERSISTENCE_CATEGORIES))
def test_permanent_persistence_categories_are_not_retryable(category: str) -> None:
    error = PersistenceError(
        category,
        PERSISTENCE_DETAILS[category],
        error_type="OperationalError",
    )

    assert error.retryable is False
