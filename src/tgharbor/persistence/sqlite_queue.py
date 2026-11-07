from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import aiosqlite

from tgharbor.core.jobs import (
    DownloadJob,
    FileLocation,
    InteractionSession,
    JobStatus,
    LibraryStats,
    NewDownloadJob,
    SourceSession,
    StoredFile,
)


class InvalidJobTransition(RuntimeError):
    """Raised when a queue operation no longer matches the job state or owner."""


class SQLiteJobQueue:
    SCHEMA_VERSION = 2

    def __init__(self, path: Path, *, busy_timeout_seconds: float = 5) -> None:
        self.path = path
        self._busy_timeout_ms = int(busy_timeout_seconds * 1000)

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self._connect() as connection:
            cursor = await connection.execute("PRAGMA user_version")
            row = await cursor.fetchone()
            version = int(row[0])
            if version > self.SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema {version} is newer than supported {self.SCHEMA_VERSION}"
                )
            if version == 0:
                await connection.executescript(
                    """
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
                    COMMIT;
                    """
                )
                version = 2
            if version == 1:
                await connection.executescript(
                    """
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
                )

    async def recover_interrupted(self, *, now: float | None = None) -> tuple[int, int]:
        timestamp = time.time() if now is None else now
        async with self._connect() as connection:
            await connection.execute(
                """
                UPDATE jobs
                SET status = ?, claimed_by = NULL, cancel_requested = 0,
                    error = NULL, updated_at = ?
                WHERE status = ? AND cancel_requested = 1
                """,
                (
                    JobStatus.CANCELLED.value,
                    timestamp,
                    JobStatus.RUNNING.value,
                ),
            )
            exhausted = await connection.execute(
                """
                UPDATE jobs
                SET status = ?, claimed_by = NULL, cancel_requested = 0,
                    error = ?, updated_at = ?
                WHERE status = ? AND cancel_requested = 0 AND attempt_count >= max_attempts
                """,
                (
                    JobStatus.FAILED.value,
                    "retry limit reached during restart recovery",
                    timestamp,
                    JobStatus.RUNNING.value,
                ),
            )
            recovered = await connection.execute(
                """
                UPDATE jobs
                SET status = ?, claimed_by = NULL, cancel_requested = 0,
                    available_at = ?, error = ?, updated_at = ?
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
        )
        async with self._connect() as connection:
            try:
                cursor = await connection.execute(
                    """
                    INSERT INTO jobs (
                        id, requester_id, source_session, source_chat, source_message_id,
                        destination_name, dedupe_key, status, max_attempts,
                        available_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    error = NULL, updated_at = ?
                WHERE id = (
                    SELECT id FROM jobs
                    WHERE status = ? AND available_at <= ? AND attempt_count < max_attempts
                    ORDER BY created_at, id
                    LIMIT 1
                ) AND status = ?
                RETURNING *
                """,
                (
                    JobStatus.RUNNING.value,
                    worker_id,
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
    ) -> DownloadJob:
        if total_bytes is not None and total_bytes < 0:
            raise ValueError("total_bytes must not be negative")
        title = _bounded_text(display_title, 160)
        original_name = _bounded_text(original_file_name, 512)
        media_type = _bounded_text(mime_type, 255)
        return await self._owned_update(
            job_id,
            worker_id,
            """
            display_title = ?, original_file_name = ?, mime_type = ?,
            total_bytes = COALESCE(?, total_bytes), updated_at = ?
            """,
            (title, original_name, media_type, total_bytes, time.time()),
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
    ) -> DownloadJob:
        relative = relative_path or Path("inbox") / output_path.name
        _validate_relative_path(relative)
        location = (
            FileLocation.ARCHIVE
            if relative.parts and relative.parts[0] == FileLocation.ARCHIVE.value
            else FileLocation.INBOX
        )
        timestamp = time.time()
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    """
                    UPDATE jobs
                    SET status = ?, output_path = ?, claimed_by = NULL,
                        cancel_requested = 0, updated_at = ?
                    WHERE id = ? AND status = ? AND claimed_by = ?
                    RETURNING *
                    """,
                    (
                        JobStatus.COMPLETED.value,
                        str(output_path),
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
                cursor = await connection.execute(
                    """
                    UPDATE stored_files
                    SET relative_path = ?, current_name = ?, location = ?,
                        updated_at = ?, last_operator_id = ?
                    WHERE job_id = ?
                    RETURNING *
                    """,
                    (
                        relative_path.as_posix(),
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
                        existing["relative_path"],
                        relative_path.as_posix(),
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
        async with self._connect() as connection:
            await connection.execute(
                """
                INSERT INTO file_audit (
                    job_id, operator_id, action, source_path, destination_path, created_at
                ) VALUES (NULL, ?, 'mkdir', NULL, ?, ?)
                """,
                (operator_id, relative_path.as_posix(), time.time()),
            )
            await connection.commit()

    async def create_interaction(
        self,
        actor_id: int,
        action: str,
        step: str,
        *,
        expires_at: datetime,
        job_id: UUID | None = None,
        pending_name: str | None = None,
        pending_directory: str | None = None,
    ) -> InteractionSession:
        if actor_id <= 0:
            raise ValueError("actor_id must be positive")
        interaction_id = uuid4()
        timestamp = time.time()
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO interactions (
                    id, actor_id, action, step, job_id, pending_name,
                    pending_directory, expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING *
                """,
                (
                    str(interaction_id),
                    actor_id,
                    _bounded_required(action, 64),
                    _bounded_required(step, 64),
                    None if job_id is None else str(job_id),
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
                  AND step IN ('await-name', 'await-directory', 'await-new-directory')
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (actor_id, time.time()),
            )
            row = await cursor.fetchone()
            return None if row is None else _row_to_interaction(row)

    async def update_interaction(
        self,
        interaction_id: UUID,
        actor_id: int,
        *,
        step: str,
        pending_name: str | None = None,
        pending_directory: str | None = None,
    ) -> InteractionSession:
        timestamp = time.time()
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                UPDATE interactions
                SET step = ?, pending_name = ?, pending_directory = ?, updated_at = ?
                WHERE id = ? AND actor_id = ? AND expires_at > ?
                RETURNING *
                """,
                (
                    _bounded_required(step, 64),
                    _bounded_text(pending_name, 512),
                    _bounded_text(pending_directory, 512),
                    timestamp,
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

    async def delete_interaction(self, interaction_id: UUID, actor_id: int) -> bool:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "DELETE FROM interactions WHERE id = ? AND actor_id = ?",
                (str(interaction_id), actor_id),
            )
            await connection.commit()
            return cursor.rowcount == 1

    async def delete_interactions_for_job(self, job_id: UUID) -> int:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "DELETE FROM interactions WHERE job_id = ?", (str(job_id),)
            )
            await connection.commit()
            return cursor.rowcount

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
        timestamp = time.time()
        if can_retry:
            return await self._owned_update(
                job_id,
                worker_id,
                """
                status = ?, claimed_by = NULL, available_at = ?, error = ?,
                progress_bytes = 0, total_bytes = NULL, updated_at = ?
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
            "status = ?, claimed_by = NULL, error = ?, updated_at = ?",
            (JobStatus.FAILED.value, error, timestamp),
        )

    async def request_cancel(self, job_id: UUID, requester_id: int) -> DownloadJob:
        timestamp = time.time()
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                UPDATE jobs
                SET status = CASE WHEN status = ? THEN ? ELSE status END,
                    cancel_requested = CASE WHEN status = ? THEN 1 ELSE cancel_requested END,
                    updated_at = ?
                WHERE id = ? AND requester_id = ? AND status IN (?, ?)
                RETURNING *
                """,
                (
                    JobStatus.PENDING.value,
                    JobStatus.CANCELLED.value,
                    JobStatus.RUNNING.value,
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

    async def mark_cancelled(self, job_id: UUID, worker_id: str) -> DownloadJob:
        return await self._owned_update(
            job_id,
            worker_id,
            "status = ?, claimed_by = NULL, cancel_requested = 0, updated_at = ?",
            (JobStatus.CANCELLED.value, time.time()),
        )

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
        connection = await aiosqlite.connect(self.path)
        connection.row_factory = aiosqlite.Row
        try:
            await connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            await connection.execute("PRAGMA foreign_keys = ON")
            yield connection
        finally:
            await connection.close()


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
    )


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
