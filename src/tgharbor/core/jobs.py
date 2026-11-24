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


class InteractionAction(StrEnum):
    KEEP = "keep"
    RENAME = "rename"
    ARCHIVE = "archive"
    RENAME_AND_ARCHIVE = "rename-and-archive"
    FILES_PAGE = "files-page"
    HISTORY_PAGE = "history-page"
    BATCH_MOVE = "batch-move"
    INTERRUPTED_VIEW = "interrupted-view"
    INTERRUPTED_CONTINUE = "interrupted-continue"


class InteractionStep(StrEnum):
    READY = "ready"
    AWAIT_NAME = "await-name"
    AWAIT_DIRECTORY = "await-directory"
    AWAIT_NEW_DIRECTORY = "await-new-directory"
    CONFIRM = "confirm"
    CONFIRM_NEW_DIRECTORY = "confirm-new-directory"
    # WORKING is retained for claims that cannot name their prior confirmation
    # and for rows written before the explicit working steps existed.
    WORKING = "working"
    WORKING_CONFIRM = "working-confirm"
    WORKING_CONFIRM_NEW_DIRECTORY = "working-confirm-new-directory"
    # Non-actionable result-delivery outbox states. A row in either state owns a
    # terminal library result that has not yet been delivered, or (for retry
    # pending) a retryable batch result whose card is not yet known visible.
    RESULT_PENDING = "result-pending"
    RETRY_PENDING = "retry-pending"


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
    batch_id: str | None = None
    batch_expected_size: int = 1
    origin_group_id: str | None = None
    channel_id: str | None = None

    def __post_init__(self) -> None:
        if self.requester_id <= 0:
            raise ValueError("requester_id must be positive")
        if not self.source_chat.strip():
            raise ValueError("source_chat must not be empty")
        if self.source_message_id <= 0:
            raise ValueError("source_message_id must be positive")
        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if self.batch_expected_size <= 0:
            raise ValueError("batch_expected_size must be positive")
        if self.batch_id is None and self.batch_expected_size != 1:
            raise ValueError("batch_expected_size requires a batch_id")
        _validate_optional_identifier(self.batch_id, "batch_id")
        _validate_optional_identifier(self.origin_group_id, "origin_group_id")
        _validate_optional_identifier(self.channel_id, "channel_id")

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
    batch_id: str | None = None
    batch_expected_size: int = 1
    started_at: datetime | None = None
    finished_at: datetime | None = None
    resume_required: bool = False
    origin_group_id: str | None = None
    channel_id: str | None = None

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
class BatchProgress:
    """Requester-scoped batch report; ``expected_size`` is None if members disagree."""

    batch_id: str
    requester_id: int
    expected_size: int | None
    total_jobs: int
    completed: int
    failed: int
    cancelled: int
    pending: int
    running: int

    @property
    def all_succeeded(self) -> bool:
        return (
            self.expected_size is not None
            and self.expected_size > 1
            and self.total_jobs == self.expected_size
            and self.completed == self.expected_size
        )


@dataclass(frozen=True, slots=True)
class BatchNotice:
    """Durable outbox candidate for one eligible batch completion notice."""

    requester_id: int
    batch_id: str


@dataclass(frozen=True, slots=True)
class InteractionSession:
    """Persisted interaction intent and the bot message that anchors its prompt.

    ``prompt_message_id`` is an internal Telegram message identifier. It is never
    placed in callback payloads or logs, and it is optional because interactions
    created before schema v4, or before their first prompt is delivered, have none.
    """

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
    batch_id: str | None = None
    prompt_message_id: int | None = None


def _validate_optional_identifier(value: str | None, label: str) -> None:
    if value is None:
        return
    if not value.strip():
        raise ValueError(f"{label} must not be empty")
    if len(value) > 128:
        raise ValueError(f"{label} is too long")
