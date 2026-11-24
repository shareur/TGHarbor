from __future__ import annotations

import contextlib
import errno
import sqlite3
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import aiosqlite

from tgharbor.core.errors import (
    EXCEPTION_TYPE_NAME_FALLBACK,
    PERSISTENCE_DETAILS,
    PersistenceError,
    safe_exception_type_name,
)
from tgharbor.core.jobs import (
    BatchNotice,
    BatchProgress,
    DownloadJob,
    FileLocation,
    InteractionAction,
    InteractionSession,
    InteractionStep,
    JobStatus,
    LibraryStats,
    NewDownloadJob,
    SourceSession,
    StoredFile,
)

_DEFAULT_NOTICE_STALE_SECONDS = 300.0
_ACTIVE_TEXT_WORKFLOW_ACTIONS = ("rename", "archive", "rename-and-archive", "batch-move")
_ACTIVE_TEXT_WORKFLOW_STEPS = (
    "await-name",
    "await-directory",
    "await-new-directory",
    "confirm",
    "confirm-new-directory",
)
_SOURCE_TEXT_WORKFLOW_STEPS = ("ready", *_ACTIVE_TEXT_WORKFLOW_STEPS)
_WORKING_STEPS = (
    InteractionStep.WORKING.value,
    InteractionStep.WORKING_CONFIRM.value,
    InteractionStep.WORKING_CONFIRM_NEW_DIRECTORY.value,
)
_PENDING_DELIVERY_STEPS = (
    InteractionStep.RESULT_PENDING.value,
    InteractionStep.RETRY_PENDING.value,
)
_NON_RESTORABLE_TARGET_STEPS = (*_WORKING_STEPS, *_PENDING_DELIVERY_STEPS)

_CREATE_SCHEMA_V4_SQL = """
BEGIN IMMEDIATE;
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
    reserved_name TEXT,
    batch_id TEXT,
    batch_expected_size INTEGER NOT NULL DEFAULT 1,
    started_at REAL,
    finished_at REAL,
    resume_required INTEGER NOT NULL DEFAULT 0,
    origin_group_id TEXT,
    channel_id TEXT
);
CREATE INDEX jobs_claimable_idx
    ON jobs(status, available_at, created_at, id);
CREATE INDEX jobs_requester_idx
    ON jobs(requester_id, created_at DESC);
CREATE UNIQUE INDEX jobs_reserved_name_idx
    ON jobs(reserved_name) WHERE reserved_name IS NOT NULL;
CREATE INDEX jobs_resume_idx
    ON jobs(requester_id, created_at, id) WHERE resume_required = 1;
CREATE INDEX jobs_batch_idx
    ON jobs(batch_id, status, id) WHERE batch_id IS NOT NULL;

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
    batch_id TEXT,
    prompt_message_id INTEGER,
    pending_name TEXT,
    pending_directory TEXT,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX interactions_actor_idx
    ON interactions(actor_id, expires_at, updated_at DESC);

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

PRAGMA user_version = 4;
COMMIT;
"""

_MIGRATE_V1_TO_V2_SQL = """
BEGIN IMMEDIATE;
ALTER TABLE jobs ADD COLUMN display_title TEXT;
ALTER TABLE jobs ADD COLUMN original_file_name TEXT;
ALTER TABLE jobs ADD COLUMN mime_type TEXT;
ALTER TABLE jobs ADD COLUMN reserved_name TEXT;
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
COMMIT;
"""

_MIGRATE_V2_TO_V3_SQL = """
BEGIN IMMEDIATE;
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
COMMIT;
"""

_MIGRATE_V3_TO_V4_SQL = """
BEGIN IMMEDIATE;
ALTER TABLE interactions ADD COLUMN prompt_message_id INTEGER;
PRAGMA user_version = 4;
COMMIT;
"""


class InvalidJobTransition(RuntimeError):
    """Raised when a queue operation no longer matches the job state or owner."""


class SQLiteJobQueue:
    SCHEMA_VERSION = 4

    def __init__(self, path: Path, *, busy_timeout_seconds: float = 15) -> None:
        self.path = path
        self._busy_timeout_ms = int(busy_timeout_seconds * 1000)

    async def initialize(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise _translate_persistence_error(error, during_connect=True) from None
        async with self._connect() as connection:
            cursor = await connection.execute("PRAGMA user_version")
            row = await cursor.fetchone()
            version = int(row[0])
            if version > self.SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema {version} is newer than supported {self.SCHEMA_VERSION}"
                )
            if version == 0:
                await self._apply_schema(connection, _CREATE_SCHEMA_V4_SQL)
                version = 4
            if version == 1:
                await self._apply_schema(connection, _MIGRATE_V1_TO_V2_SQL)
                version = 2
            if version == 2:
                await self._apply_schema(connection, _MIGRATE_V2_TO_V3_SQL)
                version = 3
            if version == 3:
                await self._apply_schema(connection, _MIGRATE_V3_TO_V4_SQL)
                version = 4
            if version != self.SCHEMA_VERSION:
                raise RuntimeError(f"database schema {version} could not be initialized")

    @staticmethod
    async def _apply_schema(connection: aiosqlite.Connection, script: str) -> None:
        """Apply one explicit schema transaction and roll it back on failure."""

        try:
            await connection.executescript(script)
        except BaseException:
            await connection.rollback()
            raise

    async def recover_interrupted(
        self,
        *,
        now: float | None = None,
        notice_stale_after_seconds: float = _DEFAULT_NOTICE_STALE_SECONDS,
    ) -> tuple[int, int]:
        """Pause eligible interrupted attempts and finish cancelled or exhausted ones.

        Returns ``(paused, exhausted)``. Paused jobs remain pending with
        ``resume_required`` set and are excluded from normal claims until a
        requester-scoped continuation clears the flag. Stale batch-notice claims
        are released on the same startup path.
        """

        if notice_stale_after_seconds < 0:
            raise ValueError("notice_stale_after_seconds must not be negative")
        timestamp = time.time() if now is None else now
        async with self._connect() as connection:
            await connection.execute(
                """
                UPDATE jobs
                SET status = ?, claimed_by = NULL, cancel_requested = 0,
                    resume_required = 0, finished_at = ?, error = NULL, updated_at = ?
                WHERE status = ? AND cancel_requested = 1
                """,
                (
                    JobStatus.CANCELLED.value,
                    timestamp,
                    timestamp,
                    JobStatus.RUNNING.value,
                ),
            )
            exhausted = await connection.execute(
                """
                UPDATE jobs
                SET status = ?, claimed_by = NULL, cancel_requested = 0,
                    resume_required = 0, error = ?, finished_at = ?, updated_at = ?
                WHERE status = ? AND cancel_requested = 0 AND attempt_count >= max_attempts
                """,
                (
                    JobStatus.FAILED.value,
                    "retry limit reached during restart recovery",
                    timestamp,
                    timestamp,
                    JobStatus.RUNNING.value,
                ),
            )
            recovered = await connection.execute(
                """
                UPDATE jobs
                SET status = ?, claimed_by = NULL, cancel_requested = 0,
                    resume_required = 1, available_at = ?, error = ?,
                    started_at = NULL, finished_at = NULL, progress_bytes = 0,
                    updated_at = ?
                WHERE status = ? AND cancel_requested = 0 AND attempt_count < max_attempts
                """,
                (
                    JobStatus.PENDING.value,
                    timestamp,
                    "interrupted by application restart",
                    timestamp,
                    JobStatus.RUNNING.value,
                ),
            )
            await connection.execute(
                """
                UPDATE batch_notifications
                SET state = 'pending', claimed_by = NULL, claimed_at = NULL,
                    updated_at = ?
                WHERE state = 'claimed'
                  AND (claimed_at IS NULL OR claimed_at <= ?)
                """,
                (timestamp, timestamp - notice_stale_after_seconds),
            )
            await connection.commit()
            return recovered.rowcount, exhausted.rowcount

    async def enqueue(self, job: NewDownloadJob) -> DownloadJob:
        created = job.created_at.timestamp()
        values = (
            str(job.id),
            job.requester_id,
            job.source_session.value,
            job.source_chat,
            job.source_message_id,
            job.destination_name,
            job.dedupe_key,
            JobStatus.PENDING.value,
            job.max_attempts,
            created,
            created,
            created,
            job.batch_id,
            job.batch_expected_size,
            job.origin_group_id,
            job.channel_id,
        )
        async with self._connect() as connection:
            try:
                cursor = await connection.execute(
                    """
                    INSERT INTO jobs (
                        id, requester_id, source_session, source_chat, source_message_id,
                        destination_name, dedupe_key, status, max_attempts,
                        available_at, created_at, updated_at,
                        batch_id, batch_expected_size, origin_group_id, channel_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    RETURNING *
                    """,
                    values,
                )
                row = await cursor.fetchone()
                await connection.commit()
            except aiosqlite.IntegrityError:
                await connection.rollback()
                cursor = await connection.execute(
                    "SELECT * FROM jobs WHERE dedupe_key = ?", (job.dedupe_key,)
                )
                row = await cursor.fetchone()
            if row is None:
                raise RuntimeError("failed to enqueue or find deduplicated job")
            return _row_to_job(row)

    async def get(self, job_id: UUID) -> DownloadJob | None:
        return await self._fetch_one("SELECT * FROM jobs WHERE id = ?", (str(job_id),))

    async def get_for_requester(self, job_id: UUID, requester_id: int) -> DownloadJob | None:
        return await self._fetch_one(
            "SELECT * FROM jobs WHERE id = ? AND requester_id = ?",
            (str(job_id), requester_id),
        )

    async def list_unreconciled_completed(
        self, *, limit: int = 100, offset: int = 0
    ) -> list[DownloadJob]:
        _validate_page(limit, offset)
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT jobs.* FROM jobs
                LEFT JOIN stored_files ON stored_files.job_id = jobs.id
                WHERE jobs.status = ? AND jobs.output_path IS NOT NULL
                  AND stored_files.job_id IS NULL
                ORDER BY jobs.created_at, jobs.id
                LIMIT ? OFFSET ?
                """,
                (JobStatus.COMPLETED.value, limit, offset),
            )
            return [_row_to_job(row) for row in await cursor.fetchall()]

    async def latest_for_requester(
        self,
        requester_id: int,
        *,
        limit: int = 10,
        offset: int = 0,
        statuses: frozenset[JobStatus] | None = None,
    ) -> list[DownloadJob]:
        _validate_page(limit, offset)
        values: list[Any] = [requester_id]
        status_clause = ""
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            status_clause = f" AND status IN ({placeholders})"
            values.extend(status.value for status in sorted(statuses, key=lambda item: item.value))
        values.extend((limit, offset))
        async with self._connect() as connection:
            cursor = await connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE requester_id = ?{status_clause}
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                tuple(values),
            )
            return [_row_to_job(row) for row in await cursor.fetchall()]

    async def list_paused_for_requester(
        self,
        requester_id: int,
        *,
        limit: int = 10,
        offset: int = 0,
    ) -> list[DownloadJob]:
        _validate_page(limit, offset)
        if requester_id <= 0:
            raise ValueError("requester_id must be positive")
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT * FROM jobs
                WHERE requester_id = ? AND status = ? AND resume_required = 1
                ORDER BY created_at, id
                LIMIT ? OFFSET ?
                """,
                (requester_id, JobStatus.PENDING.value, limit, offset),
            )
            return [_row_to_job(row) for row in await cursor.fetchall()]

    async def count_paused_for_requester(self, requester_id: int) -> int:
        if requester_id <= 0:
            raise ValueError("requester_id must be positive")
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT COUNT(*) FROM jobs
                WHERE requester_id = ? AND status = ? AND resume_required = 1
                """,
                (requester_id, JobStatus.PENDING.value),
            )
            row = await cursor.fetchone()
        assert row is not None
        return int(row[0])

    async def continue_paused_for_requester(
        self,
        requester_id: int,
        job_ids: Sequence[UUID] | None = None,
        *,
        now: float | None = None,
    ) -> int:
        """Clear pause state for all or selected requester-owned paused jobs.

        Already resumed or foreign jobs are ignored, so repeated and partial
        calls are idempotent. Retry accounting is preserved.
        """

        if requester_id <= 0:
            raise ValueError("requester_id must be positive")
        timestamp = time.time() if now is None else now
        selection = ""
        selected: tuple[str, ...] = ()
        if job_ids is not None:
            selected = tuple(str(job_id) for job_id in job_ids)
            if not selected:
                return 0
            placeholders = ", ".join("?" for _ in selected)
            selection = f" AND id IN ({placeholders})"
        async with self._connect() as connection:
            cursor = await connection.execute(
                f"""
                UPDATE jobs
                SET resume_required = 0, available_at = ?, error = NULL,
                    started_at = NULL, updated_at = ?
                WHERE requester_id = ? AND status = ? AND resume_required = 1{selection}
                """,
                (timestamp, timestamp, requester_id, JobStatus.PENDING.value, *selected),
            )
            await connection.commit()
            return cursor.rowcount

    async def batch_progress_for_requester(
        self,
        requester_id: int,
        batch_id: str,
    ) -> BatchProgress | None:
        if requester_id <= 0:
            raise ValueError("requester_id must be positive")
        batch = _opaque_identifier(batch_id)
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT COUNT(*) AS total_jobs,
                       COALESCE(SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END), 0)
                           AS completed,
                       COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0)
                           AS failed,
                       COALESCE(SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END), 0)
                           AS cancelled,
                       COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0)
                           AS pending,
                       COALESCE(SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END), 0)
                           AS running,
                       MIN(batch_expected_size) AS min_expected,
                       MAX(batch_expected_size) AS max_expected
                FROM jobs
                WHERE requester_id = ? AND batch_id = ?
                """,
                (requester_id, batch),
            )
            row = await cursor.fetchone()
        if row is None or int(row["total_jobs"]) == 0:
            return None
        minimum = row["min_expected"]
        maximum = row["max_expected"]
        expected = int(minimum) if minimum is not None and minimum == maximum else None
        return BatchProgress(
            batch_id=batch,
            requester_id=requester_id,
            expected_size=expected,
            total_jobs=int(row["total_jobs"]),
            completed=int(row["completed"]),
            failed=int(row["failed"]),
            cancelled=int(row["cancelled"]),
            pending=int(row["pending"]),
            running=int(row["running"]),
        )

    async def resolve_for_requester(self, requester_id: int, identifier: str) -> DownloadJob | None:
        compact = identifier.strip().lower().replace("-", "")
        if (
            len(compact) < 4
            or len(compact) > 32
            or any(character not in "0123456789abcdef" for character in compact)
        ):
            return None
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT * FROM jobs
                WHERE requester_id = ? AND REPLACE(LOWER(id), '-', '') LIKE ?
                ORDER BY created_at DESC, id DESC
                LIMIT 2
                """,
                (requester_id, f"{compact}%"),
            )
            rows = await cursor.fetchall()
        return _row_to_job(rows[0]) if len(rows) == 1 else None

    async def claim_next(self, worker_id: str, *, now: float | None = None) -> DownloadJob | None:
        if not worker_id.strip():
            raise ValueError("worker_id must not be empty")
        timestamp = time.time() if now is None else now
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                UPDATE jobs
                SET status = ?, claimed_by = ?, attempt_count = attempt_count + 1,
                    error = NULL, resume_required = 0, started_at = ?,
                    finished_at = NULL, updated_at = ?
                WHERE id = (
                    SELECT id FROM jobs
                    WHERE status = ? AND resume_required = 0
                      AND available_at <= ? AND attempt_count < max_attempts
                    ORDER BY created_at, id
                    LIMIT 1
                ) AND status = ? AND resume_required = 0
                RETURNING *
                """,
                (
                    JobStatus.RUNNING.value,
                    worker_id,
                    timestamp,
                    timestamp,
                    JobStatus.PENDING.value,
                    timestamp,
                    JobStatus.PENDING.value,
                ),
            )
            row = await cursor.fetchone()
            await connection.commit()
            return None if row is None else _row_to_job(row)

    async def update_progress(
        self,
        job_id: UUID,
        worker_id: str,
        current: int,
        total: int | None,
    ) -> DownloadJob:
        if current < 0 or (total is not None and (total < 0 or current > total)):
            raise ValueError("invalid progress values")
        return await self._owned_update(
            job_id,
            worker_id,
            "progress_bytes = ?, total_bytes = ?, updated_at = ?",
            (current, total, time.time()),
        )

    async def update_metadata(
        self,
        job_id: UUID,
        worker_id: str,
        *,
        display_title: str | None,
        original_file_name: str | None,
        mime_type: str | None,
        total_bytes: int | None,
        origin_group_id: str | None = None,
        channel_id: str | None = None,
    ) -> DownloadJob:
        if total_bytes is not None and total_bytes < 0:
            raise ValueError("total_bytes must not be negative")
        title = _bounded_text(display_title, 160)
        original_name = _bounded_text(original_file_name, 512)
        media_type = _bounded_text(mime_type, 255)
        origin_group = _bounded_text(origin_group_id, 128)
        source_channel = _bounded_text(channel_id, 128)
        return await self._owned_update(
            job_id,
            worker_id,
            """
            display_title = ?, original_file_name = ?, mime_type = ?,
            total_bytes = COALESCE(?, total_bytes),
            origin_group_id = COALESCE(?, origin_group_id),
            channel_id = COALESCE(?, channel_id), updated_at = ?
            """,
            (
                title,
                original_name,
                media_type,
                total_bytes,
                origin_group,
                source_channel,
                time.time(),
            ),
        )

    async def reserve_name(self, job_id: UUID, worker_id: str, desired_name: str) -> DownloadJob:
        desired = _bounded_required(desired_name, 255)
        timestamp = time.time()
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    """
                    SELECT * FROM jobs
                    WHERE id = ? AND status = ? AND claimed_by = ?
                    """,
                    (str(job_id), JobStatus.RUNNING.value, worker_id),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise InvalidJobTransition(
                        f"job {job_id} is not running for worker {worker_id}"
                    )
                if row["reserved_name"]:
                    await connection.commit()
                    return _row_to_job(row)
                cursor = await connection.execute(
                    "SELECT 1 FROM stored_files WHERE relative_path = ?",
                    ((Path("inbox") / desired).as_posix(),),
                )
                catalog_conflict = await cursor.fetchone() is not None
                cursor = await connection.execute(
                    "SELECT 1 FROM jobs WHERE reserved_name = ? LIMIT 1", (desired,)
                )
                reservation_conflict = await cursor.fetchone() is not None
                reserved = (
                    _with_identifier(desired, job_id.hex[:8])
                    if catalog_conflict or reservation_conflict
                    else desired
                )
                cursor = await connection.execute(
                    """
                    UPDATE jobs SET reserved_name = ?, updated_at = ?
                    WHERE id = ? AND status = ? AND claimed_by = ?
                    RETURNING *
                    """,
                    (
                        reserved,
                        timestamp,
                        str(job_id),
                        JobStatus.RUNNING.value,
                        worker_id,
                    ),
                )
                updated = await cursor.fetchone()
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        assert updated is not None
        return _row_to_job(updated)

    async def reserve_legacy_name(self, job_id: UUID, desired_name: str) -> DownloadJob:
        desired = _bounded_required(desired_name, 255)
        timestamp = time.time()
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    """
                    SELECT jobs.* FROM jobs
                    LEFT JOIN stored_files ON stored_files.job_id = jobs.id
                    WHERE jobs.id = ? AND jobs.status = ? AND stored_files.job_id IS NULL
                    """,
                    (str(job_id), JobStatus.COMPLETED.value),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise InvalidJobTransition(f"job {job_id} is not an unreconciled completed job")
                if row["reserved_name"]:
                    await connection.commit()
                    return _row_to_job(row)
                cursor = await connection.execute(
                    "SELECT 1 FROM stored_files WHERE relative_path = ?",
                    ((Path("inbox") / desired).as_posix(),),
                )
                catalog_conflict = await cursor.fetchone() is not None
                cursor = await connection.execute(
                    "SELECT 1 FROM jobs WHERE reserved_name = ? LIMIT 1", (desired,)
                )
                reservation_conflict = await cursor.fetchone() is not None
                reserved = (
                    _with_identifier(desired, job_id.hex[:8])
                    if catalog_conflict or reservation_conflict
                    else desired
                )
                cursor = await connection.execute(
                    """
                    UPDATE jobs SET reserved_name = ?, updated_at = ?
                    WHERE id = ? AND status = ?
                    RETURNING *
                    """,
                    (
                        reserved,
                        timestamp,
                        str(job_id),
                        JobStatus.COMPLETED.value,
                    ),
                )
                updated = await cursor.fetchone()
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        assert updated is not None
        return _row_to_job(updated)

    async def complete(
        self,
        job_id: UUID,
        worker_id: str,
        output_path: Path,
        *,
        relative_path: Path | None = None,
        now: float | None = None,
    ) -> DownloadJob:
        """Compatibility alias for :meth:`mark_completed`."""

        return await self.mark_completed(
            job_id,
            worker_id,
            output_path,
            relative_path=relative_path,
            now=now,
        )

    async def mark_completed(
        self,
        job_id: UUID,
        worker_id: str,
        output_path: Path,
        *,
        relative_path: Path | None = None,
        now: float | None = None,
    ) -> DownloadJob:
        relative = relative_path or Path("inbox") / output_path.name
        _validate_relative_path(relative)
        location = (
            FileLocation.ARCHIVE
            if relative.parts and relative.parts[0] == FileLocation.ARCHIVE.value
            else FileLocation.INBOX
        )
        timestamp = time.time() if now is None else now
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    """
                    UPDATE jobs
                    SET status = ?, output_path = ?, claimed_by = NULL,
                        cancel_requested = 0, finished_at = ?, updated_at = ?
                    WHERE id = ? AND status = ? AND claimed_by = ?
                    RETURNING *
                    """,
                    (
                        JobStatus.COMPLETED.value,
                        str(output_path),
                        timestamp,
                        timestamp,
                        str(job_id),
                        JobStatus.RUNNING.value,
                        worker_id,
                    ),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise InvalidJobTransition(
                        f"job {job_id} is not running for worker {worker_id}"
                    )
                current_name = relative.name
                original_name = row["original_file_name"] or current_name
                size_bytes = row["total_bytes"] or row["progress_bytes"] or 0
                await connection.execute(
                    """
                    INSERT INTO stored_files (
                        job_id, requester_id, relative_path, original_name, current_name,
                        display_title, mime_type, size_bytes, location,
                        created_at, updated_at, last_operator_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    ON CONFLICT(job_id) DO UPDATE SET
                        relative_path = excluded.relative_path,
                        current_name = excluded.current_name,
                        display_title = excluded.display_title,
                        mime_type = excluded.mime_type,
                        size_bytes = excluded.size_bytes,
                        location = excluded.location,
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(job_id),
                        int(row["requester_id"]),
                        relative.as_posix(),
                        original_name,
                        current_name,
                        row["display_title"],
                        row["mime_type"],
                        int(size_bytes),
                        location.value,
                        timestamp,
                        timestamp,
                    ),
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        return _row_to_job(row)

    async def list_files(self, *, limit: int = 10, offset: int = 0) -> list[StoredFile]:
        _validate_page(limit, offset)
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT * FROM stored_files
                ORDER BY updated_at DESC, job_id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
            return [_row_to_stored_file(row) for row in await cursor.fetchall()]

    async def list_files_for_batch(
        self,
        requester_id: int,
        batch_id: str,
    ) -> list[StoredFile]:
        if requester_id <= 0:
            raise ValueError("requester_id must be positive")
        batch = _opaque_identifier(batch_id)
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT stored_files.* FROM stored_files
                JOIN jobs ON jobs.id = stored_files.job_id
                WHERE jobs.requester_id = ? AND jobs.batch_id = ? AND jobs.status = ?
                ORDER BY stored_files.created_at, stored_files.job_id
                """,
                (requester_id, batch, JobStatus.COMPLETED.value),
            )
            return [_row_to_stored_file(row) for row in await cursor.fetchall()]

    async def get_file(self, job_id: UUID) -> StoredFile | None:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "SELECT * FROM stored_files WHERE job_id = ?", (str(job_id),)
            )
            row = await cursor.fetchone()
            return None if row is None else _row_to_stored_file(row)

    async def register_legacy_file(
        self,
        job_id: UUID,
        relative_path: Path,
        output_path: Path,
        size_bytes: int,
    ) -> StoredFile:
        _validate_relative_path(relative_path)
        if relative_path.parts[0] != FileLocation.INBOX.value:
            raise ValueError("legacy files must be reconciled into inbox")
        if size_bytes < 0:
            raise ValueError("size_bytes must not be negative")
        timestamp = time.time()
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    "SELECT * FROM stored_files WHERE job_id = ?", (str(job_id),)
                )
                existing = await cursor.fetchone()
                if existing is not None:
                    await connection.commit()
                    return _row_to_stored_file(existing)
                cursor = await connection.execute(
                    "SELECT * FROM jobs WHERE id = ? AND status = ?",
                    (str(job_id), JobStatus.COMPLETED.value),
                )
                job = await cursor.fetchone()
                if job is None:
                    raise InvalidJobTransition(f"job {job_id} is not completed")
                original_name = job["original_file_name"] or relative_path.name
                cursor = await connection.execute(
                    """
                    INSERT INTO stored_files (
                        job_id, requester_id, relative_path, original_name, current_name,
                        display_title, mime_type, size_bytes, location,
                        created_at, updated_at, last_operator_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    RETURNING *
                    """,
                    (
                        str(job_id),
                        int(job["requester_id"]),
                        relative_path.as_posix(),
                        original_name,
                        relative_path.name,
                        job["display_title"],
                        job["mime_type"],
                        size_bytes,
                        FileLocation.INBOX.value,
                        timestamp,
                        timestamp,
                    ),
                )
                stored = await cursor.fetchone()
                await connection.execute(
                    """
                    UPDATE jobs SET output_path = ?, reserved_name = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (str(output_path), relative_path.name, timestamp, str(job_id)),
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        assert stored is not None
        return _row_to_stored_file(stored)

    async def resolve_file(self, identifier: str) -> StoredFile | None:
        compact = identifier.strip().lower().replace("-", "")
        if (
            len(compact) < 4
            or len(compact) > 32
            or any(character not in "0123456789abcdef" for character in compact)
        ):
            return None
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT * FROM stored_files
                WHERE REPLACE(LOWER(job_id), '-', '') LIKE ?
                ORDER BY updated_at DESC, job_id DESC
                LIMIT 2
                """,
                (f"{compact}%",),
            )
            rows = await cursor.fetchall()
        return _row_to_stored_file(rows[0]) if len(rows) == 1 else None

    async def record_file_move(
        self,
        job_id: UUID,
        operator_id: int,
        relative_path: Path,
        output_path: Path,
        *,
        action: str,
    ) -> StoredFile:
        if operator_id <= 0:
            raise ValueError("operator_id must be positive")
        if action not in {"rename", "archive", "rename-and-archive", "reconcile"}:
            raise ValueError("unsupported file action")
        _validate_relative_path(relative_path)
        location = (
            FileLocation.ARCHIVE
            if relative_path.parts[0] == FileLocation.ARCHIVE.value
            else FileLocation.INBOX
        )
        timestamp = time.time()
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    "SELECT * FROM stored_files WHERE job_id = ?", (str(job_id),)
                )
                existing = await cursor.fetchone()
                if existing is None:
                    raise KeyError(str(job_id))
                source_path = str(existing["relative_path"])
                destination_path = relative_path.as_posix()
                cursor = await connection.execute(
                    """
                    UPDATE stored_files
                    SET relative_path = ?, current_name = ?, location = ?,
                        updated_at = ?, last_operator_id = ?
                    WHERE job_id = ?
                    RETURNING *
                    """,
                    (
                        destination_path,
                        relative_path.name,
                        location.value,
                        timestamp,
                        operator_id,
                        str(job_id),
                    ),
                )
                updated = await cursor.fetchone()
                await connection.execute(
                    "UPDATE jobs SET output_path = ?, updated_at = ? WHERE id = ?",
                    (str(output_path), timestamp, str(job_id)),
                )
                # Reconciliation and retries can replay an identical move. An
                # unchanged normalized source and destination is not a new audit
                # event, so repeated recovery never appends duplicate rows.
                if source_path != destination_path:
                    await connection.execute(
                        """
                        INSERT INTO file_audit (
                            job_id, operator_id, action, source_path, destination_path, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(job_id),
                            operator_id,
                            action,
                            source_path,
                            destination_path,
                            timestamp,
                        ),
                    )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        assert updated is not None
        return _row_to_stored_file(updated)

    async def record_directory_creation(self, operator_id: int, relative_path: Path) -> None:
        if operator_id <= 0:
            raise ValueError("operator_id must be positive")
        _validate_relative_path(relative_path)
        normalized = relative_path.as_posix()
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                # Creating an already-existing identical directory is an
                # idempotent replay, not a new audit event. The exact normalized
                # root-relative value is checked and inserted in one writer
                # transaction, so concurrent creates cannot duplicate it.
                cursor = await connection.execute(
                    """
                    SELECT 1 FROM file_audit
                    WHERE action = 'mkdir' AND destination_path = ?
                    LIMIT 1
                    """,
                    (normalized,),
                )
                existing = await cursor.fetchone()
                if existing is None:
                    await connection.execute(
                        """
                        INSERT INTO file_audit (
                            job_id, operator_id, action, source_path, destination_path, created_at
                        ) VALUES (NULL, ?, 'mkdir', NULL, ?, ?)
                        """,
                        (operator_id, normalized, time.time()),
                    )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def create_interaction(
        self,
        actor_id: int,
        action: str,
        step: str,
        *,
        expires_at: datetime,
        job_id: UUID | None = None,
        batch_id: str | None = None,
        prompt_message_id: int | None = None,
        pending_name: str | None = None,
        pending_directory: str | None = None,
    ) -> InteractionSession:
        if actor_id <= 0:
            raise ValueError("actor_id must be positive")
        interaction_id = uuid4()
        timestamp = time.time()
        batch = None if batch_id is None else _opaque_identifier(batch_id)
        prompt = _message_identifier(prompt_message_id)
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO interactions (
                    id, actor_id, action, step, job_id, batch_id, prompt_message_id,
                    pending_name, pending_directory, expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING *
                """,
                (
                    str(interaction_id),
                    actor_id,
                    _bounded_required(action, 64),
                    _bounded_required(step, 64),
                    None if job_id is None else str(job_id),
                    batch,
                    prompt,
                    _bounded_text(pending_name, 512),
                    _bounded_text(pending_directory, 512),
                    expires_at.timestamp(),
                    timestamp,
                    timestamp,
                ),
            )
            row = await cursor.fetchone()
            await connection.commit()
        assert row is not None
        return _row_to_interaction(row)

    async def get_interaction(self, interaction_id: UUID) -> InteractionSession | None:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "SELECT * FROM interactions WHERE id = ?", (str(interaction_id),)
            )
            row = await cursor.fetchone()
            return None if row is None else _row_to_interaction(row)

    async def latest_interaction_for_actor(self, actor_id: int) -> InteractionSession | None:
        if actor_id <= 0:
            raise ValueError("actor_id must be positive")
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT * FROM interactions
                WHERE actor_id = ? AND expires_at > ?
                  AND step IN (
                      'await-name', 'await-directory', 'await-new-directory',
                      'confirm', 'confirm-new-directory'
                  )
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (actor_id, time.time()),
            )
            row = await cursor.fetchone()
            return None if row is None else _row_to_interaction(row)

    async def consume_page_interaction(
        self,
        interaction_id: UUID,
        actor_id: int,
        *,
        action: str,
        step: str,
    ) -> InteractionSession | None:
        """Delete and return one exact, authorized page interaction.

        The ``DELETE ... RETURNING`` statement is the ownership point for a page
        transition. A concurrent caller, an expired row, a different actor, or a
        changed action/step observes ``None`` without mutating anything. The
        winner owns the token before creating successor interactions or editing
        the card.
        """

        if actor_id <= 0:
            raise ValueError("actor_id must be positive")
        timestamp = time.time()
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                DELETE FROM interactions
                WHERE id = ? AND actor_id = ? AND action = ? AND step = ?
                  AND expires_at > ?
                RETURNING *
                """,
                (
                    str(interaction_id),
                    actor_id,
                    _bounded_required(action, 64),
                    _bounded_required(step, 64),
                    timestamp,
                ),
            )
            row = await cursor.fetchone()
            await connection.commit()
        return None if row is None else _row_to_interaction(row)

    async def restore_page_interaction(
        self,
        session: InteractionSession,
        *,
        now: float | None = None,
    ) -> bool:
        """Restore one consumed page interaction exactly, if still unexpired.

        The original ID, actor, action, step, pending data, prompt anchor, and
        timestamps are reinstated so the old visible page remains operable. If
        the original expired while delivery was attempted, nothing is restored
        and the caller keeps the replacement sessions removed.
        """

        if session.actor_id <= 0:
            raise ValueError("actor_id must be positive")
        timestamp = time.time() if now is None else now
        if session.expires_at.timestamp() <= timestamp:
            return False
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO interactions (
                    id, actor_id, action, step, job_id, batch_id, prompt_message_id,
                    pending_name, pending_directory, expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    str(session.id),
                    session.actor_id,
                    _bounded_required(session.action, 64),
                    _bounded_required(session.step, 64),
                    None if session.job_id is None else str(session.job_id),
                    None if session.batch_id is None else _opaque_identifier(session.batch_id),
                    _message_identifier(session.prompt_message_id),
                    _bounded_text(session.pending_name, 512),
                    _bounded_text(session.pending_directory, 512),
                    session.expires_at.timestamp(),
                    session.created_at.timestamp(),
                    session.updated_at.timestamp(),
                ),
            )
            restored = cursor.rowcount == 1
            await connection.commit()
        return restored

    async def update_interaction(
        self,
        interaction_id: UUID,
        actor_id: int,
        *,
        step: str,
        pending_name: str | None = None,
        pending_directory: str | None = None,
        prompt_message_id: int | None = None,
    ) -> InteractionSession:
        """Update an owned interaction; a provided prompt anchor is persisted.

        ``prompt_message_id=None`` leaves the stored anchor unchanged so ordinary
        step transitions do not silently lose the bot message a prompt lives in.
        """

        timestamp = time.time()
        prompt = _message_identifier(prompt_message_id)
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                UPDATE interactions
                SET step = ?, pending_name = ?, pending_directory = ?, updated_at = ?,
                    prompt_message_id = COALESCE(?, prompt_message_id)
                WHERE id = ? AND actor_id = ? AND expires_at > ?
                RETURNING *
                """,
                (
                    _bounded_required(step, 64),
                    _bounded_text(pending_name, 512),
                    _bounded_text(pending_directory, 512),
                    timestamp,
                    prompt,
                    str(interaction_id),
                    actor_id,
                    timestamp,
                ),
            )
            row = await cursor.fetchone()
            await connection.commit()
        if row is None:
            raise KeyError(str(interaction_id))
        return _row_to_interaction(row)

    async def compare_and_set_prompt_message_id(
        self,
        interaction_id: UUID,
        actor_id: int,
        *,
        expected_prompt_message_id: int | None,
        prompt_message_id: int,
    ) -> InteractionSession | None:
        """Bind a prompt anchor only when the stored anchor still matches.

        ``expected_prompt_message_id`` may be ``None`` for a prompt that has not
        been bound yet. A stale writer, missing row, expired row, or different
        owner returns ``None`` without changing the stored anchor.
        """

        if actor_id <= 0:
            raise ValueError("actor_id must be positive")
        expected = _message_identifier(expected_prompt_message_id)
        prompt = _message_identifier(prompt_message_id)
        if prompt is None:
            raise ValueError("prompt_message_id must be positive")
        timestamp = time.time()
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    """
                    UPDATE interactions
                    SET prompt_message_id = ?, updated_at = ?
                    WHERE id = ? AND actor_id = ? AND expires_at > ?
                      AND prompt_message_id IS ?
                    RETURNING *
                    """,
                    (
                        prompt,
                        timestamp,
                        str(interaction_id),
                        actor_id,
                        timestamp,
                        expected,
                    ),
                )
                row = await cursor.fetchone()
                if row is None:
                    await connection.rollback()
                    return None
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        return _row_to_interaction(row)

    async def activate_text_workflow(
        self,
        interaction_id: UUID,
        actor_id: int,
        *,
        action: str,
        expected_steps: Sequence[str],
        step: str,
        pending_name: str | None = None,
        pending_directory: str | None = None,
        prompt_message_id: int | None = None,
    ) -> InteractionSession | None:
        """Atomically activate one text workflow and supersede older ones.

        The keeper row must exist for the actor, be unexpired, match the expected
        action and one of the expected source steps, and transition to an active
        awaiting or confirmation step. Only after that transition succeeds does
        the same transaction delete other active text workflows for the actor.
        Page interactions and ready completion buttons are never deleted.
        """

        if actor_id <= 0:
            raise ValueError("actor_id must be positive")
        if step not in _ACTIVE_TEXT_WORKFLOW_STEPS:
            raise ValueError("step must be an active text workflow step")
        expected = tuple(dict.fromkeys(expected_steps))
        if not expected or any(item not in _SOURCE_TEXT_WORKFLOW_STEPS for item in expected):
            raise ValueError("expected_steps must contain text workflow steps")
        prompt = _message_identifier(prompt_message_id)
        timestamp = time.time()
        expected_placeholders = ", ".join("?" for _ in expected)
        action_placeholders = ", ".join("?" for _ in _ACTIVE_TEXT_WORKFLOW_ACTIONS)
        step_placeholders = ", ".join("?" for _ in _ACTIVE_TEXT_WORKFLOW_STEPS)
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    f"""
                    UPDATE interactions
                    SET step = ?, pending_name = ?, pending_directory = ?, updated_at = ?,
                        prompt_message_id = COALESCE(?, prompt_message_id)
                    WHERE id = ? AND actor_id = ? AND expires_at > ? AND action = ?
                      AND step IN ({expected_placeholders})
                    RETURNING *
                    """,
                    (
                        step,
                        _bounded_text(pending_name, 512),
                        _bounded_text(pending_directory, 512),
                        timestamp,
                        prompt,
                        str(interaction_id),
                        actor_id,
                        timestamp,
                        _bounded_required(action, 64),
                        *expected,
                    ),
                )
                row = await cursor.fetchone()
                if row is None:
                    await connection.rollback()
                    return None
                await connection.execute(
                    f"""
                    DELETE FROM interactions
                    WHERE actor_id = ? AND id != ?
                      AND action IN ({action_placeholders})
                      AND step IN ({step_placeholders})
                    """,
                    (
                        actor_id,
                        str(interaction_id),
                        *_ACTIVE_TEXT_WORKFLOW_ACTIONS,
                        *_ACTIVE_TEXT_WORKFLOW_STEPS,
                    ),
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        return _row_to_interaction(row)

    async def claim_working_interaction(
        self,
        interaction_id: UUID,
        actor_id: int,
        *,
        action: str,
        expected_steps: Sequence[str],
    ) -> InteractionSession | None:
        """Claim one confirmation for library work with a WORKING transition.

        Exactly one concurrent caller can claim the same row. The transition
        records which confirmation was claimed by using an explicit
        ``working-confirm`` or ``working-confirm-new-directory`` step, so a
        restart can restore a safe prior state instead of guessing. Losers
        observe the winner's working state and must not mutate the interaction
        or its card.
        """

        if actor_id <= 0:
            raise ValueError("actor_id must be positive")
        expected = tuple(dict.fromkeys(expected_steps))
        if not expected or any(item not in _ACTIVE_TEXT_WORKFLOW_STEPS for item in expected):
            raise ValueError("expected_steps must contain active text workflow steps")
        timestamp = time.time()
        placeholders = ", ".join("?" for _ in expected)
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    f"""
                    UPDATE interactions
                    SET step = CASE
                            WHEN step = ? THEN ?
                            WHEN step = ? THEN ?
                            ELSE ?
                        END,
                        updated_at = ?
                    WHERE id = ? AND actor_id = ? AND expires_at > ? AND action = ?
                      AND step IN ({placeholders})
                    RETURNING *
                    """,
                    (
                        InteractionStep.CONFIRM.value,
                        InteractionStep.WORKING_CONFIRM.value,
                        InteractionStep.CONFIRM_NEW_DIRECTORY.value,
                        InteractionStep.WORKING_CONFIRM_NEW_DIRECTORY.value,
                        InteractionStep.WORKING.value,
                        timestamp,
                        str(interaction_id),
                        actor_id,
                        timestamp,
                        _bounded_required(action, 64),
                        *expected,
                    ),
                )
                row = await cursor.fetchone()
                if row is None:
                    await connection.rollback()
                    return None
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        return _row_to_interaction(row)

    async def restore_working_interaction(
        self,
        interaction_id: UUID,
        actor_id: int,
        *,
        step: str,
        prompt_message_id: int | None,
        expires_at: datetime,
        pending_name: str | None = None,
        pending_directory: str | None = None,
    ) -> InteractionSession | None:
        """Return any owned WORKING interaction to a recoverable state.

        Only the current working owner can restore it. The prior prompt anchor
        is written explicitly, including NULL, so a working-card fallback cannot
        leave the restored state pointing at a replacement message. A lost claim
        or a newer workflow returns ``None`` and is never overwritten.

        The caller supplies a renewed actionable expiry. A working claim that
        outlived its original expiry is still restored, because an expired
        working row is stranded rather than safely stale.
        """

        if actor_id <= 0:
            raise ValueError("actor_id must be positive")
        if step in _NON_RESTORABLE_TARGET_STEPS:
            raise ValueError("restore target must be actionable")
        timestamp = time.time()
        placeholders = ", ".join("?" for _ in _WORKING_STEPS)
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    f"""
                    UPDATE interactions
                    SET step = ?, pending_name = ?, pending_directory = ?,
                        prompt_message_id = ?, expires_at = ?, updated_at = ?
                    WHERE id = ? AND actor_id = ?
                      AND step IN ({placeholders})
                    RETURNING *
                    """,
                    (
                        _bounded_required(step, 64),
                        _bounded_text(pending_name, 512),
                        _bounded_text(pending_directory, 512),
                        _message_identifier(prompt_message_id),
                        expires_at.timestamp(),
                        timestamp,
                        str(interaction_id),
                        actor_id,
                        *_WORKING_STEPS,
                    ),
                )
                row = await cursor.fetchone()
                if row is None:
                    await connection.rollback()
                    return None
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        return _row_to_interaction(row)

    async def recover_working_interactions(
        self, *, expires_at: datetime, now: float | None = None
    ) -> list[InteractionSession]:
        """Restore persisted working interactions and return their new states.

        Explicit ``working-confirm`` and ``working-confirm-new-directory`` steps
        carry the exact prior confirmation, including pending input and prompt
        anchor. A legacy generic ``working`` row is restored to ``ready`` and
        cleared so the original completion/retry action can be chosen again
        instead of guessing which destructive confirmation was claimed.

        Every working claim is recovered, including one whose original expiry
        passed while it was stranded. The caller supplies a renewed actionable
        expiry so the restored card is clickable. The states are restored in one
        ``BEGIN IMMEDIATE`` transaction that returns every changed row, so the
        adapter can make the visible cards actionable before callback handlers
        accept events. Returning the new session data avoids a second, racy read.
        """

        timestamp = time.time() if now is None else now
        renewed_expiry = expires_at.timestamp()
        placeholders = ", ".join("?" for _ in _WORKING_STEPS)
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    f"""
                    UPDATE interactions
                    SET step = CASE
                            WHEN step = ? THEN ?
                            WHEN step = ? THEN ?
                            ELSE ?
                        END,
                        pending_name = CASE
                            WHEN step IN (?, ?) THEN pending_name
                            ELSE NULL
                        END,
                        pending_directory = CASE
                            WHEN step IN (?, ?) THEN pending_directory
                            ELSE NULL
                        END,
                        expires_at = ?,
                        updated_at = ?
                    WHERE step IN ({placeholders})
                    RETURNING *
                    """,
                    (
                        InteractionStep.WORKING_CONFIRM.value,
                        InteractionStep.CONFIRM.value,
                        InteractionStep.WORKING_CONFIRM_NEW_DIRECTORY.value,
                        InteractionStep.CONFIRM_NEW_DIRECTORY.value,
                        InteractionStep.READY.value,
                        InteractionStep.WORKING_CONFIRM.value,
                        InteractionStep.WORKING_CONFIRM_NEW_DIRECTORY.value,
                        InteractionStep.WORKING_CONFIRM.value,
                        InteractionStep.WORKING_CONFIRM_NEW_DIRECTORY.value,
                        renewed_expiry,
                        timestamp,
                        *_WORKING_STEPS,
                    ),
                )
                rows = await cursor.fetchall()
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        return [_row_to_interaction(row) for row in rows]

    async def persist_result_pending(
        self,
        interaction_id: UUID,
        actor_id: int,
        *,
        action: str,
        expected_steps: Sequence[str],
        job_id: UUID | None = None,
    ) -> InteractionSession | None:
        """Keep one terminal library result until it is actually delivered.

        Only the exact owned working claim can move to ``result-pending``. The
        same transaction deletes sibling completion-action rows for the job, so
        a stale Keep/Rename/Archive control cannot act after the operation
        reached its terminal result. No actionable-expiry predicate applies:
        an expired but undelivered result stays recoverable.
        """

        return await self._transition_to_pending_delivery(
            interaction_id,
            actor_id,
            action=action,
            expected_steps=expected_steps,
            step=InteractionStep.RESULT_PENDING.value,
            job_id=job_id,
        )

    async def persist_retry_pending(
        self,
        interaction_id: UUID,
        actor_id: int,
        *,
        action: str,
        expected_steps: Sequence[str],
    ) -> InteractionSession | None:
        """Keep a retryable batch result until its Retry card is visible.

        The row is not actionable until its actual delivered anchor is bound
        and it transitions to ``ready``; no expiry predicate applies.
        """

        return await self._transition_to_pending_delivery(
            interaction_id,
            actor_id,
            action=action,
            expected_steps=expected_steps,
            step=InteractionStep.RETRY_PENDING.value,
            job_id=None,
        )

    async def _transition_to_pending_delivery(
        self,
        interaction_id: UUID,
        actor_id: int,
        *,
        action: str,
        expected_steps: Sequence[str],
        step: str,
        job_id: UUID | None,
    ) -> InteractionSession | None:
        if actor_id <= 0:
            raise ValueError("actor_id must be positive")
        expected = tuple(dict.fromkeys(expected_steps))
        if not expected or any(item not in _WORKING_STEPS for item in expected):
            raise ValueError("expected_steps must contain working steps")
        if step not in _PENDING_DELIVERY_STEPS:
            raise ValueError("step must be a pending-delivery step")
        timestamp = time.time()
        placeholders = ", ".join("?" for _ in expected)
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    f"""
                    UPDATE interactions
                    SET step = ?, pending_name = NULL, pending_directory = NULL,
                        updated_at = ?
                    WHERE id = ? AND actor_id = ? AND action = ?
                      AND step IN ({placeholders})
                    RETURNING *
                    """,
                    (
                        step,
                        timestamp,
                        str(interaction_id),
                        actor_id,
                        _bounded_required(action, 64),
                        *expected,
                    ),
                )
                row = await cursor.fetchone()
                if row is None:
                    await connection.rollback()
                    return None
                if job_id is not None:
                    # Sibling completion controls are stale, but an undelivered
                    # result or retry row is never deleted before its delivery.
                    pending_placeholders = ", ".join("?" for _ in _PENDING_DELIVERY_STEPS)
                    await connection.execute(
                        f"""
                        DELETE FROM interactions
                        WHERE job_id = ? AND id != ?
                          AND step NOT IN ({pending_placeholders})
                        """,
                        (str(job_id), str(interaction_id), *_PENDING_DELIVERY_STEPS),
                    )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        return _row_to_interaction(row)

    async def bind_pending_prompt_message_id(
        self,
        interaction_id: UUID,
        actor_id: int,
        *,
        expected_step: str,
        expected_prompt_message_id: int | None,
        prompt_message_id: int,
    ) -> InteractionSession | None:
        """Bind a replacement anchor for a pending result without expiry.

        Actionable expiry must not reject an undelivered pending result, so this
        dedicated compare-and-swap only requires the exact actor, step, and
        expected anchor (including NULL).
        """

        if actor_id <= 0:
            raise ValueError("actor_id must be positive")
        if expected_step not in _PENDING_DELIVERY_STEPS:
            raise ValueError("expected_step must be a pending-delivery step")
        expected = _message_identifier(expected_prompt_message_id)
        prompt = _message_identifier(prompt_message_id)
        if prompt is None:
            raise ValueError("prompt_message_id must be positive")
        timestamp = time.time()
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    """
                    UPDATE interactions
                    SET prompt_message_id = ?, updated_at = ?
                    WHERE id = ? AND actor_id = ? AND step = ?
                      AND prompt_message_id IS ?
                    RETURNING *
                    """,
                    (
                        prompt,
                        timestamp,
                        str(interaction_id),
                        actor_id,
                        expected_step,
                        expected,
                    ),
                )
                row = await cursor.fetchone()
                if row is None:
                    await connection.rollback()
                    return None
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        return _row_to_interaction(row)

    async def finish_result_delivery(
        self,
        interaction_id: UUID,
        actor_id: int,
        *,
        action: str,
        expected_step: str,
        expected_prompt_message_id: int | None,
    ) -> bool:
        """Delete one delivered terminal result with an expected-state CAS.

        The expected step and anchor make cleanup a single-winner operation, and
        pending-delivery rows ignore actionable expiry so an expired result that
        is delivered now is still cleaned up exactly once.
        """

        if actor_id <= 0:
            raise ValueError("actor_id must be positive")
        if expected_step not in _PENDING_DELIVERY_STEPS:
            raise ValueError("expected_step must be a pending-delivery step")
        expected = _message_identifier(expected_prompt_message_id)
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                DELETE FROM interactions
                WHERE id = ? AND actor_id = ? AND action = ? AND step = ?
                  AND prompt_message_id IS ?
                """,
                (
                    str(interaction_id),
                    actor_id,
                    _bounded_required(action, 64),
                    expected_step,
                    expected,
                ),
            )
            deleted = cursor.rowcount == 1
            await connection.commit()
        return deleted

    async def mark_retry_ready(
        self,
        interaction_id: UUID,
        actor_id: int,
        *,
        action: str,
        expected_prompt_message_id: int | None,
        expires_at: datetime,
    ) -> InteractionSession | None:
        """Make a delivered Retry card actionable with an expected-anchor CAS.

        The retry-pending row deliberately ignores actionable expiry so an
        undelivered retry remains recoverable. Promoting it to ``ready`` must
        therefore install a fresh actionable expiry; otherwise a long-delayed
        recovery would produce a ready row that every callback rejects.
        """

        if actor_id <= 0:
            raise ValueError("actor_id must be positive")
        expected = _message_identifier(expected_prompt_message_id)
        timestamp = time.time()
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    """
                    UPDATE interactions
                    SET step = ?, pending_name = NULL, pending_directory = NULL,
                        expires_at = ?, updated_at = ?
                    WHERE id = ? AND actor_id = ? AND action = ? AND step = ?
                      AND prompt_message_id IS ?
                    RETURNING *
                    """,
                    (
                        InteractionStep.READY.value,
                        expires_at.timestamp(),
                        timestamp,
                        str(interaction_id),
                        actor_id,
                        _bounded_required(action, 64),
                        InteractionStep.RETRY_PENDING.value,
                        expected,
                    ),
                )
                row = await cursor.fetchone()
                if row is None:
                    await connection.rollback()
                    return None
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        return _row_to_interaction(row)

    async def list_pending_delivery_interactions(self) -> list[InteractionSession]:
        """Return every undelivered result row regardless of actionable expiry.

        There is no fixed cap: a durable outbox must make progress on later rows
        even when an earlier row repeatedly fails, and the caller already
        isolates each row's delivery failure.
        """

        placeholders = ", ".join("?" for _ in _PENDING_DELIVERY_STEPS)
        async with self._connect() as connection:
            cursor = await connection.execute(
                f"""
                SELECT * FROM interactions
                WHERE step IN ({placeholders})
                ORDER BY updated_at, id
                """,
                (*_PENDING_DELIVERY_STEPS,),
            )
            rows = await cursor.fetchall()
        return [_row_to_interaction(row) for row in rows]

    async def delete_interaction(self, interaction_id: UUID, actor_id: int) -> bool:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "DELETE FROM interactions WHERE id = ? AND actor_id = ?",
                (str(interaction_id), actor_id),
            )
            await connection.commit()
            return cursor.rowcount == 1

    async def delete_interactions_for_job(self, job_id: UUID) -> int:
        """Delete a job's actionable rows while retaining undelivered results."""

        placeholders = ", ".join("?" for _ in _PENDING_DELIVERY_STEPS)
        async with self._connect() as connection:
            cursor = await connection.execute(
                f"""
                DELETE FROM interactions
                WHERE job_id = ? AND step NOT IN ({placeholders})
                """,
                (str(job_id), *_PENDING_DELIVERY_STEPS),
            )
            await connection.commit()
            return cursor.rowcount

    async def archive_directory_recency(self) -> dict[str, float]:
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT destination_path AS path, action AS kind, created_at AS used_at
                FROM file_audit
                WHERE destination_path IS NOT NULL
                UNION ALL
                SELECT relative_path AS path, 'file' AS kind, updated_at AS used_at
                FROM stored_files
                WHERE location = 'archive'
                """
            )
            rows = await cursor.fetchall()
        recency: dict[str, float] = {}
        for row in rows:
            parts = Path(str(row["path"])).parts
            if len(parts) < 2 or parts[0] != FileLocation.ARCHIVE.value:
                continue
            directory_parts = parts[1:] if row["kind"] == "mkdir" else parts[1:-1]
            if not directory_parts:
                continue
            key = Path(*directory_parts).as_posix()
            used_at = float(row["used_at"])
            recency[key] = max(recency.get(key, used_at), used_at)
        return recency

    async def library_stats(self) -> LibraryStats:
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT COUNT(*) AS file_count,
                       COALESCE(SUM(size_bytes), 0) AS total_bytes,
                       SUM(CASE WHEN location = 'inbox' THEN 1 ELSE 0 END) AS inbox_count,
                       SUM(CASE WHEN location = 'archive' THEN 1 ELSE 0 END) AS archive_count
                FROM stored_files
                """
            )
            row = await cursor.fetchone()
        assert row is not None
        return LibraryStats(
            file_count=int(row["file_count"]),
            total_bytes=int(row["total_bytes"]),
            inbox_count=int(row["inbox_count"] or 0),
            archive_count=int(row["archive_count"] or 0),
        )

    async def fail_or_retry(
        self,
        job_id: UUID,
        worker_id: str,
        error: str,
        *,
        retryable: bool,
        retry_delay_seconds: float = 0,
        now: float | None = None,
    ) -> DownloadJob:
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must not be negative")
        current = await self.get(job_id)
        if (
            current is None
            or current.status is not JobStatus.RUNNING
            or current.claimed_by != worker_id
        ):
            raise InvalidJobTransition(f"job {job_id} is not running for worker {worker_id}")
        can_retry = retryable and current.attempt_count < current.max_attempts
        timestamp = time.time() if now is None else now
        if can_retry:
            return await self._owned_update(
                job_id,
                worker_id,
                """
                status = ?, claimed_by = NULL, available_at = ?, error = ?,
                progress_bytes = 0, total_bytes = NULL, started_at = NULL,
                finished_at = NULL, updated_at = ?
                """,
                (
                    JobStatus.PENDING.value,
                    timestamp + retry_delay_seconds,
                    error,
                    timestamp,
                ),
            )
        return await self._owned_update(
            job_id,
            worker_id,
            """
            status = ?, claimed_by = NULL, error = ?, finished_at = ?, updated_at = ?
            """,
            (JobStatus.FAILED.value, error, timestamp, timestamp),
        )

    async def request_cancel(self, job_id: UUID, requester_id: int) -> DownloadJob:
        timestamp = time.time()
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                UPDATE jobs
                SET status = CASE WHEN status = ? THEN ? ELSE status END,
                    cancel_requested = CASE WHEN status = ? THEN 1 ELSE cancel_requested END,
                    resume_required = CASE WHEN status = ? THEN 0 ELSE resume_required END,
                    finished_at = CASE WHEN status = ? THEN ? ELSE finished_at END,
                    updated_at = ?
                WHERE id = ? AND requester_id = ? AND status IN (?, ?)
                RETURNING *
                """,
                (
                    JobStatus.PENDING.value,
                    JobStatus.CANCELLED.value,
                    JobStatus.RUNNING.value,
                    JobStatus.PENDING.value,
                    JobStatus.PENDING.value,
                    timestamp,
                    timestamp,
                    str(job_id),
                    requester_id,
                    JobStatus.PENDING.value,
                    JobStatus.RUNNING.value,
                ),
            )
            row = await cursor.fetchone()
            await connection.commit()
        if row is None:
            existing = await self.get_for_requester(job_id, requester_id)
            if existing is None:
                raise KeyError(str(job_id))
            return existing
        return _row_to_job(row)

    async def is_cancel_requested(self, job_id: UUID, worker_id: str) -> bool:
        job = await self.get(job_id)
        if job is None or job.status is not JobStatus.RUNNING or job.claimed_by != worker_id:
            raise InvalidJobTransition(f"job {job_id} is not running for worker {worker_id}")
        return job.cancel_requested

    async def mark_cancelled(
        self,
        job_id: UUID,
        worker_id: str,
        *,
        now: float | None = None,
    ) -> DownloadJob:
        timestamp = time.time() if now is None else now
        return await self._owned_update(
            job_id,
            worker_id,
            """
            status = ?, claimed_by = NULL, cancel_requested = 0,
            resume_required = 0, finished_at = ?, updated_at = ?
            """,
            (JobStatus.CANCELLED.value, timestamp, timestamp),
        )

    async def list_claimable_batch_notifications(
        self,
        *,
        now: float | None = None,
        stale_after_seconds: float = _DEFAULT_NOTICE_STALE_SECONDS,
        limit: int = 20,
    ) -> list[BatchNotice]:
        """Return a bounded page of durable batch-notice outbox candidates.

        Candidates are eligible successful batches whose notice row is absent,
        pending, or claimed by a sender that is now stale. Sent notices and
        fresh claims are omitted, but callers must still claim each candidate:
        eligibility and the single-winner transition are rechecked atomically.
        """

        if stale_after_seconds < 0:
            raise ValueError("stale_after_seconds must not be negative")
        if limit <= 0:
            raise ValueError("limit must be positive")
        timestamp = time.time() if now is None else now
        stale_before = timestamp - stale_after_seconds
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT jobs.requester_id AS requester_id,
                       jobs.batch_id AS batch_id
                FROM jobs
                WHERE jobs.batch_id IS NOT NULL
                GROUP BY jobs.requester_id, jobs.batch_id
                HAVING MIN(jobs.batch_expected_size) = MAX(jobs.batch_expected_size)
                   AND MIN(jobs.batch_expected_size) > 1
                   AND COUNT(*) = MIN(jobs.batch_expected_size)
                   AND SUM(
                       CASE WHEN jobs.status = 'completed' THEN 1 ELSE 0 END
                   ) = COUNT(*)
                   AND NOT EXISTS (
                       SELECT 1
                       FROM batch_notifications AS notice
                       WHERE notice.requester_id = jobs.requester_id
                         AND notice.batch_id = jobs.batch_id
                         AND (
                             notice.state = 'sent'
                             OR (
                                 notice.state = 'claimed'
                                 AND (
                                     notice.claimed_at IS NULL
                                     OR notice.claimed_at > ?
                                 )
                             )
                         )
                   )
                ORDER BY MIN(jobs.finished_at), jobs.requester_id, jobs.batch_id
                LIMIT ?
                """,
                (stale_before, limit),
            )
            rows = await cursor.fetchall()
        return [
            BatchNotice(
                requester_id=int(row["requester_id"]),
                batch_id=str(row["batch_id"]),
            )
            for row in rows
        ]

    async def claim_batch_notification(
        self,
        batch_id: str,
        requester_id: int,
        *,
        claimed_by: str,
        now: float | None = None,
        stale_after_seconds: float = _DEFAULT_NOTICE_STALE_SECONDS,
    ) -> bool:
        """Claim the single completion notice for an eligible successful batch.

        The transaction checks batch eligibility and the durable notice state
        together, so concurrent workers cannot both claim the same notice. A
        sent notice is terminal; a failed sender must release its claim.
        """

        batch = _opaque_identifier(batch_id)
        if requester_id <= 0:
            raise ValueError("requester_id must be positive")
        if not claimed_by.strip():
            raise ValueError("claimed_by must not be empty")
        if stale_after_seconds < 0:
            raise ValueError("stale_after_seconds must not be negative")
        timestamp = time.time() if now is None else now
        stale_before = timestamp - stale_after_seconds
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    """
                    SELECT COUNT(*) AS total_jobs,
                           COALESCE(
                               SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END), 0
                           ) AS completed,
                           MIN(batch_expected_size) AS min_expected,
                           MAX(batch_expected_size) AS max_expected
                    FROM jobs
                    WHERE requester_id = ? AND batch_id = ?
                    """,
                    (requester_id, batch),
                )
                counts = await cursor.fetchone()
                eligible = False
                if counts is not None:
                    total = int(counts["total_jobs"])
                    completed = int(counts["completed"])
                    minimum = counts["min_expected"]
                    maximum = counts["max_expected"]
                    eligible = (
                        minimum is not None
                        and minimum == maximum
                        and int(minimum) > 1
                        and total == int(minimum)
                        and completed == total
                    )
                if not eligible:
                    await connection.commit()
                    return False

                cursor = await connection.execute(
                    """
                    SELECT state, claimed_at
                    FROM batch_notifications
                    WHERE requester_id = ? AND batch_id = ?
                    """,
                    (requester_id, batch),
                )
                existing = await cursor.fetchone()
                if existing is not None:
                    if existing["state"] == "sent":
                        await connection.commit()
                        return False
                    if existing["state"] == "claimed" and (
                        existing["claimed_at"] is not None
                        and float(existing["claimed_at"]) > stale_before
                    ):
                        await connection.commit()
                        return False
                    await connection.execute(
                        """
                        UPDATE batch_notifications
                        SET state = 'claimed', claimed_by = ?, claimed_at = ?,
                            sent_at = NULL, updated_at = ?
                        WHERE requester_id = ? AND batch_id = ?
                        """,
                        (claimed_by, timestamp, timestamp, requester_id, batch),
                    )
                else:
                    await connection.execute(
                        """
                        INSERT INTO batch_notifications (
                            requester_id, batch_id, state, claimed_by, claimed_at,
                            sent_at, created_at, updated_at
                        ) VALUES (?, ?, 'claimed', ?, ?, NULL, ?, ?)
                        """,
                        (
                            requester_id,
                            batch,
                            claimed_by,
                            timestamp,
                            timestamp,
                            timestamp,
                        ),
                    )
                await connection.commit()
                return True
            except BaseException:
                await connection.rollback()
                raise

    async def mark_batch_notification_sent(
        self,
        batch_id: str,
        requester_id: int,
        *,
        claimed_by: str,
        now: float | None = None,
    ) -> bool:
        batch = _opaque_identifier(batch_id)
        if requester_id <= 0:
            raise ValueError("requester_id must be positive")
        if not claimed_by.strip():
            raise ValueError("claimed_by must not be empty")
        timestamp = time.time() if now is None else now
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    """
                    UPDATE batch_notifications
                    SET state = 'sent', sent_at = ?, claimed_by = NULL,
                        claimed_at = NULL, updated_at = ?
                    WHERE requester_id = ? AND batch_id = ?
                      AND state = 'claimed' AND claimed_by = ?
                    """,
                    (timestamp, timestamp, requester_id, batch, claimed_by),
                )
                changed = cursor.rowcount == 1
                if not changed:
                    cursor = await connection.execute(
                        """
                        SELECT state FROM batch_notifications
                        WHERE requester_id = ? AND batch_id = ?
                        """,
                        (requester_id, batch),
                    )
                    row = await cursor.fetchone()
                    changed = row is not None and row["state"] == "sent"
                await connection.commit()
                return changed
            except BaseException:
                await connection.rollback()
                raise

    async def active_batch_move_interaction(
        self,
        requester_id: int,
        batch_id: str,
        *,
        now: float | None = None,
    ) -> InteractionSession | None:
        """Return the requester's live Move-all interaction for one batch."""

        if requester_id <= 0:
            raise ValueError("requester_id must be positive")
        batch = _opaque_identifier(batch_id)
        timestamp = time.time() if now is None else now
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT * FROM interactions
                WHERE actor_id = ? AND batch_id = ? AND action = ? AND expires_at > ?
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (requester_id, batch, InteractionAction.BATCH_MOVE.value, timestamp),
            )
            row = await cursor.fetchone()
            return None if row is None else _row_to_interaction(row)

    async def ensure_batch_move_interaction(
        self,
        requester_id: int,
        batch_id: str,
        *,
        expires_at: datetime,
        now: float | None = None,
    ) -> InteractionSession:
        """Create or reuse exactly one Move-all interaction for a batch.

        A send may have succeeded before its durable mark failed, or a process
        may have crashed after creating the interaction but before sending. The
        persisted row is therefore reused, not treated as delivery evidence. Its
        identity and callback payload stay stable across at-least-once retries.
        """

        if requester_id <= 0:
            raise ValueError("requester_id must be positive")
        batch = _opaque_identifier(batch_id)
        timestamp = time.time() if now is None else now
        expiry = expires_at.timestamp()
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    """
                    SELECT * FROM interactions
                    WHERE actor_id = ? AND batch_id = ? AND action = ?
                    ORDER BY updated_at DESC, id DESC
                    LIMIT 1
                    """,
                    (requester_id, batch, InteractionAction.BATCH_MOVE.value),
                )
                row = await cursor.fetchone()
                if row is None:
                    cursor = await connection.execute(
                        """
                        INSERT INTO interactions (
                            id, actor_id, action, step, job_id, batch_id,
                            pending_name, pending_directory, expires_at,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, NULL, ?, NULL, NULL, ?, ?, ?)
                        RETURNING *
                        """,
                        (
                            str(uuid4()),
                            requester_id,
                            InteractionAction.BATCH_MOVE.value,
                            InteractionStep.READY.value,
                            batch,
                            expiry,
                            timestamp,
                            timestamp,
                        ),
                    )
                else:
                    cursor = await connection.execute(
                        """
                        UPDATE interactions
                        SET step = ?, pending_name = NULL, pending_directory = NULL,
                            expires_at = ?, updated_at = ?
                        WHERE id = ?
                        RETURNING *
                        """,
                        (
                            InteractionStep.READY.value,
                            expiry,
                            timestamp,
                            row["id"],
                        ),
                    )
                row = await cursor.fetchone()
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        assert row is not None
        return _row_to_interaction(row)

    async def release_batch_notification(
        self,
        batch_id: str,
        requester_id: int,
        *,
        claimed_by: str,
        now: float | None = None,
    ) -> bool:
        batch = _opaque_identifier(batch_id)
        if requester_id <= 0:
            raise ValueError("requester_id must be positive")
        if not claimed_by.strip():
            raise ValueError("claimed_by must not be empty")
        timestamp = time.time() if now is None else now
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    """
                    UPDATE batch_notifications
                    SET state = 'pending', claimed_by = NULL, claimed_at = NULL,
                        updated_at = ?
                    WHERE requester_id = ? AND batch_id = ?
                      AND state = 'claimed' AND claimed_by = ?
                    """,
                    (timestamp, requester_id, batch, claimed_by),
                )
                released = cursor.rowcount == 1
                if not released:
                    cursor = await connection.execute(
                        """
                        SELECT state FROM batch_notifications
                        WHERE requester_id = ? AND batch_id = ?
                        """,
                        (requester_id, batch),
                    )
                    row = await cursor.fetchone()
                    released = row is not None and row["state"] == "pending"
                await connection.commit()
                return released
            except BaseException:
                await connection.rollback()
                raise

    async def recover_stale_batch_notifications(
        self,
        *,
        now: float | None = None,
        stale_after_seconds: float = _DEFAULT_NOTICE_STALE_SECONDS,
    ) -> int:
        if stale_after_seconds < 0:
            raise ValueError("stale_after_seconds must not be negative")
        timestamp = time.time() if now is None else now
        stale_before = timestamp - stale_after_seconds
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                UPDATE batch_notifications
                SET state = 'pending', claimed_by = NULL, claimed_at = NULL,
                    updated_at = ?
                WHERE state = 'claimed'
                  AND (claimed_at IS NULL OR claimed_at <= ?)
                """,
                (timestamp, stale_before),
            )
            await connection.commit()
            return cursor.rowcount

    async def _owned_update(
        self,
        job_id: UUID,
        worker_id: str,
        assignments: str,
        values: tuple[Any, ...],
    ) -> DownloadJob:
        async with self._connect() as connection:
            cursor = await connection.execute(
                f"""
                UPDATE jobs SET {assignments}
                WHERE id = ? AND status = ? AND claimed_by = ?
                RETURNING *
                """,
                (*values, str(job_id), JobStatus.RUNNING.value, worker_id),
            )
            row = await cursor.fetchone()
            await connection.commit()
        if row is None:
            raise InvalidJobTransition(f"job {job_id} is not running for worker {worker_id}")
        return _row_to_job(row)

    async def _fetch_one(self, query: str, values: tuple[Any, ...]) -> DownloadJob | None:
        async with self._connect() as connection:
            cursor = await connection.execute(query, values)
            row = await cursor.fetchone()
            return None if row is None else _row_to_job(row)

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        try:
            connection = await aiosqlite.connect(self.path)
        except (sqlite3.Error, OSError) as error:
            raise _translate_persistence_error(error, during_connect=True) from None
        try:
            connection.row_factory = aiosqlite.Row
            try:
                await connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
                await connection.execute("PRAGMA foreign_keys = ON")
            except (sqlite3.Error, OSError) as error:
                raise _translate_persistence_error(error, during_connect=True) from None
            try:
                yield connection
            except (sqlite3.Error, OSError) as error:
                raise _translate_persistence_error(error) from None
        finally:
            with contextlib.suppress(Exception):
                await connection.close()


_SQLITE_PRIMARY_CATEGORIES = {
    3: "permission",  # SQLITE_PERM
    5: "busy",  # SQLITE_BUSY
    6: "busy",  # SQLITE_LOCKED
    8: "read-only",  # SQLITE_READONLY
    10: "io",  # SQLITE_IOERR
    11: "corrupt",  # SQLITE_CORRUPT
    13: "disk-full",  # SQLITE_FULL
    14: "open",  # SQLITE_CANTOPEN
    17: "schema",  # SQLITE_SCHEMA
    19: "integrity",  # SQLITE_CONSTRAINT
    21: "adapter",  # SQLITE_MISUSE
    23: "permission",  # SQLITE_AUTH
    26: "corrupt",  # SQLITE_NOTADB
}

_ERRNO_CATEGORY_NAMES = (
    ("EACCES", "permission"),
    ("EPERM", "permission"),
    ("EROFS", "read-only"),
    ("ENOSPC", "disk-full"),
    ("EDQUOT", "disk-full"),
    ("EIO", "io"),
    ("ENOENT", "open"),
    ("ENOTDIR", "open"),
    ("EMFILE", "open"),
    ("ENFILE", "open"),
)

_MESSAGE_CATEGORIES = (
    ("database is locked", "busy"),
    ("database table is locked", "busy"),
    ("database schema is locked", "busy"),
    ("database is busy", "busy"),
    ("unable to open database file", "open"),
    ("unable to open database", "open"),
    ("cannot open database", "open"),
    ("disk i/o error", "io"),
    ("database or disk is full", "disk-full"),
    ("disk is full", "disk-full"),
    ("readonly database", "read-only"),
    ("database disk image is malformed", "corrupt"),
    ("file is not a database", "corrupt"),
    ("database corruption", "corrupt"),
    ("already exists", "schema"),
    ("no such table", "schema"),
    ("no such column", "schema"),
    ("duplicate column name", "schema"),
    ("syntax error", "schema"),
    ("constraint failed", "integrity"),
    ("constraint violation", "integrity"),
)


def _translate_persistence_error(
    error: BaseException, *, during_connect: bool = False
) -> PersistenceError:
    """Translate a storage exception without retaining its message or cause."""
    if isinstance(error, PersistenceError):
        return error
    category = _persistence_category(error, during_connect=during_connect)
    return PersistenceError(
        category,
        PERSISTENCE_DETAILS[category],
        error_type=safe_exception_type_name(error, fallback=EXCEPTION_TYPE_NAME_FALLBACK),
        code=_persistence_code(error),
    )


def _persistence_category(error: BaseException, *, during_connect: bool) -> str:
    sqlite_code = getattr(error, "sqlite_errorcode", None)
    if isinstance(sqlite_code, int) and not isinstance(sqlite_code, bool):
        category = _SQLITE_PRIMARY_CATEGORIES.get(sqlite_code & 0xFF)
        if category is not None:
            return category
    error_number = getattr(error, "errno", None)
    if isinstance(error_number, int) and not isinstance(error_number, bool):
        for name, category in _ERRNO_CATEGORY_NAMES:
            if getattr(errno, name, None) == error_number:
                return category
    message = str(error).lower()
    for needle, category in _MESSAGE_CATEGORIES:
        if needle in message:
            return category
    if isinstance(error, sqlite3.InterfaceError | sqlite3.ProgrammingError):
        return "adapter"
    if isinstance(error, OSError) or during_connect:
        return "open" if during_connect else "io"
    return "database"


def _persistence_code(error: BaseException) -> int | None:
    sqlite_code = getattr(error, "sqlite_errorcode", None)
    if isinstance(sqlite_code, int) and not isinstance(sqlite_code, bool):
        return sqlite_code & 0xFF
    error_number = getattr(error, "errno", None)
    if isinstance(error_number, int) and not isinstance(error_number, bool):
        return error_number
    return None


def _row_to_job(row: aiosqlite.Row) -> DownloadJob:
    return DownloadJob(
        id=UUID(row["id"]),
        requester_id=int(row["requester_id"]),
        source_session=SourceSession(row["source_session"]),
        source_chat=str(row["source_chat"]),
        source_message_id=int(row["source_message_id"]),
        destination_name=row["destination_name"],
        status=JobStatus(row["status"]),
        attempt_count=int(row["attempt_count"]),
        max_attempts=int(row["max_attempts"]),
        progress_bytes=int(row["progress_bytes"]),
        total_bytes=None if row["total_bytes"] is None else int(row["total_bytes"]),
        output_path=None if row["output_path"] is None else Path(row["output_path"]),
        error=row["error"],
        available_at=datetime.fromtimestamp(float(row["available_at"]), UTC),
        created_at=datetime.fromtimestamp(float(row["created_at"]), UTC),
        updated_at=datetime.fromtimestamp(float(row["updated_at"]), UTC),
        claimed_by=row["claimed_by"],
        cancel_requested=bool(row["cancel_requested"]),
        display_title=row["display_title"],
        original_file_name=row["original_file_name"],
        mime_type=row["mime_type"],
        reserved_name=row["reserved_name"],
        batch_id=row["batch_id"],
        batch_expected_size=int(row["batch_expected_size"]),
        started_at=_optional_datetime(row["started_at"]),
        finished_at=_optional_datetime(row["finished_at"]),
        resume_required=bool(row["resume_required"]),
        origin_group_id=row["origin_group_id"],
        channel_id=row["channel_id"],
    )


def _row_to_stored_file(row: aiosqlite.Row) -> StoredFile:
    return StoredFile(
        job_id=UUID(row["job_id"]),
        requester_id=int(row["requester_id"]),
        relative_path=Path(row["relative_path"]),
        original_name=str(row["original_name"]),
        current_name=str(row["current_name"]),
        display_title=row["display_title"],
        mime_type=row["mime_type"],
        size_bytes=int(row["size_bytes"]),
        location=FileLocation(row["location"]),
        created_at=datetime.fromtimestamp(float(row["created_at"]), UTC),
        updated_at=datetime.fromtimestamp(float(row["updated_at"]), UTC),
        last_operator_id=(
            None if row["last_operator_id"] is None else int(row["last_operator_id"])
        ),
    )


def _row_to_interaction(row: aiosqlite.Row) -> InteractionSession:
    return InteractionSession(
        id=UUID(row["id"]),
        actor_id=int(row["actor_id"]),
        action=str(row["action"]),
        step=str(row["step"]),
        job_id=None if row["job_id"] is None else UUID(row["job_id"]),
        pending_name=row["pending_name"],
        pending_directory=row["pending_directory"],
        expires_at=datetime.fromtimestamp(float(row["expires_at"]), UTC),
        created_at=datetime.fromtimestamp(float(row["created_at"]), UTC),
        updated_at=datetime.fromtimestamp(float(row["updated_at"]), UTC),
        batch_id=row["batch_id"],
        prompt_message_id=(
            None if row["prompt_message_id"] is None else int(row["prompt_message_id"])
        ),
    )


def _optional_datetime(value: Any) -> datetime | None:
    return None if value is None else datetime.fromtimestamp(float(value), UTC)


def _message_identifier(value: int | None) -> int | None:
    if value is None:
        return None
    if value <= 0:
        raise ValueError("prompt_message_id must be positive")
    return int(value)


def _opaque_identifier(value: str) -> str:
    if not value.strip() or len(value) > 128:
        raise ValueError("batch_id must be a bounded non-empty opaque identifier")
    return value


def _validate_page(limit: int, offset: int) -> None:
    if limit <= 0 or limit > 100:
        raise ValueError("limit must be between 1 and 100")
    if offset < 0:
        raise ValueError("offset must not be negative")


def _bounded_text(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())
    return normalized[:limit] or None


def _bounded_required(value: str, limit: int) -> str:
    bounded = _bounded_text(value, limit)
    if bounded is None:
        raise ValueError("value must not be empty")
    return bounded


def _validate_relative_path(path: Path) -> None:
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("path must be a safe relative path")
    if path.parts[0] not in {FileLocation.INBOX.value, FileLocation.ARCHIVE.value}:
        raise ValueError("path must be below inbox or archive")


def _with_identifier(name: str, identifier: str) -> str:
    path = Path(name)
    return f"{path.stem}--{identifier}{path.suffix}"
