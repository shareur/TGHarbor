from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4


def utc_now() -> datetime:
    return datetime.now(UTC)


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SourceSession(StrEnum):
    BOT = "bot"
    USER = "user"


class FileLocation(StrEnum):
    INBOX = "inbox"
    ARCHIVE = "archive"


@dataclass(frozen=True, slots=True)
class NewDownloadJob:
    requester_id: int
    source_session: SourceSession
    source_chat: str
    source_message_id: int
    destination_name: str | None = None
    max_attempts: int = 3
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if self.requester_id <= 0:
            raise ValueError("requester_id must be positive")
        if not self.source_chat.strip():
            raise ValueError("source_chat must not be empty")
        if self.source_message_id <= 0:
            raise ValueError("source_message_id must be positive")
        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be positive")

    @property
    def dedupe_key(self) -> str:
        return ":".join(
            (
                str(self.requester_id),
                self.source_session.value,
                self.source_chat,
                str(self.source_message_id),
            )
        )


@dataclass(frozen=True, slots=True)
class DownloadJob:
    id: UUID
    requester_id: int
    source_session: SourceSession
    source_chat: str
    source_message_id: int
    destination_name: str | None
    status: JobStatus
    attempt_count: int
    max_attempts: int
    progress_bytes: int
    total_bytes: int | None
    output_path: Path | None
    error: str | None
    available_at: datetime
    created_at: datetime
    updated_at: datetime
    claimed_by: str | None
    cancel_requested: bool
    display_title: str | None = None
    original_file_name: str | None = None
    mime_type: str | None = None
    reserved_name: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}

    @property
    def short_id(self) -> str:
        return self.id.hex[:8]


@dataclass(frozen=True, slots=True)
class StoredFile:
    job_id: UUID
    requester_id: int
    relative_path: Path
    original_name: str
    current_name: str
    display_title: str | None
    mime_type: str | None
    size_bytes: int
    location: FileLocation
    created_at: datetime
    updated_at: datetime
    last_operator_id: int | None

    @property
    def short_id(self) -> str:
        return self.job_id.hex[:8]


@dataclass(frozen=True, slots=True)
class LibraryStats:
    file_count: int
    total_bytes: int
    inbox_count: int
    archive_count: int


@dataclass(frozen=True, slots=True)
class InteractionSession:
    id: UUID
    actor_id: int
    action: str
    step: str
    job_id: UUID | None
    pending_name: str | None
    pending_directory: str | None
    expires_at: datetime
    created_at: datetime
    updated_at: datetime
