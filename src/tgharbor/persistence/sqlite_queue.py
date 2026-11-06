from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import aiosqlite

from tgharbor.core.jobs import DownloadJob, JobStatus, NewDownloadJob, SourceSession


class InvalidJobTransition(RuntimeError):
    """Raised when a queue operation no longer matches the job state or owner."""


class SQLiteJobQueue:
    SCHEMA_VERSION = 1

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
            await connection.commit()

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

    async def latest_for_requester(
        self, requester_id: int, *, limit: int = 10
    ) -> list[DownloadJob]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT * FROM jobs
                WHERE requester_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (requester_id, limit),
            )
            return [_row_to_job(row) for row in await cursor.fetchall()]

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

    async def complete(self, job_id: UUID, worker_id: str, output_path: Path) -> DownloadJob:
        return await self._owned_update(
            job_id,
            worker_id,
            """
            status = ?, output_path = ?, claimed_by = NULL,
            cancel_requested = 0, updated_at = ?
            """,
            (JobStatus.COMPLETED.value, str(output_path), time.time()),
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
    )
