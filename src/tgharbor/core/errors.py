from __future__ import annotations

from types import MappingProxyType
from typing import Final

EXCEPTION_TYPE_NAME_MAX_LENGTH: Final = 64
EXCEPTION_TYPE_NAME_FALLBACK: Final = "Exception"

PERSISTENCE_DETAILS: Final = MappingProxyType(
    {
        "busy": "database remained locked",
        "open": "database could not be opened",
        "io": "database input/output failed",
        "disk-full": "database storage is full",
        "permission": "database access was denied",
        "read-only": "database is read-only",
        "corrupt": "database image is corrupt",
        "schema": "database schema is incompatible",
        "integrity": "database constraint was violated",
        "adapter": "database adapter failed",
        "database": "database operation failed",
    }
)

# Categories an operator can plausibly recover from without replacing the store.
TRANSIENT_PERSISTENCE_CATEGORIES: Final = frozenset(
    {"busy", "io", "disk-full", "permission", "read-only", "open"}
)
# Categories that require operator intervention before the same job can succeed.
PERMANENT_PERSISTENCE_CATEGORIES: Final = frozenset(
    {"corrupt", "schema", "integrity", "adapter", "database"}
)

if TRANSIENT_PERSISTENCE_CATEGORIES | PERMANENT_PERSISTENCE_CATEGORIES != frozenset(
    PERSISTENCE_DETAILS
):
    raise RuntimeError("persistence categories must partition PERSISTENCE_DETAILS")
if TRANSIENT_PERSISTENCE_CATEGORIES & PERMANENT_PERSISTENCE_CATEGORIES:
    raise RuntimeError("persistence categories must not overlap")


class DownloadError(RuntimeError):
    """Base class for expected provider failures."""

    retryable = False
    retry_after_seconds = 0.0


class PermanentDownloadError(DownloadError):
    """A source cannot be downloaded by retrying the same job."""


class RetryableDownloadError(DownloadError):
    """A temporary provider failure that may succeed later."""

    retryable = True

    def __init__(self, message: str, *, retry_after_seconds: float = 0) -> None:
        super().__init__(message)
        if retry_after_seconds < 0:
            raise ValueError("retry_after_seconds must not be negative")
        self.retry_after_seconds = retry_after_seconds


# Fixed human-readable details that the built-in provider may show to requesters.
TELEGRAM_NO_RESULT_DETAIL: Final = "Telegram returned no downloadable media"
TELEGRAM_MESSAGE_NOT_FOUND_DETAIL: Final = "Telegram message was not found or is inaccessible"
TELEGRAM_MESSAGE_NO_MEDIA_DETAIL: Final = "Telegram message has no downloadable media"
TELEGRAM_RATE_LIMIT_DETAIL: Final = "Telegram rate limit"
TELEGRAM_CONNECTION_FAILURE_DETAIL: Final = "temporary Telegram connection failure"
TELEGRAM_SOURCE_INACCESSIBLE_DETAIL: Final = "Telegram source is invalid or inaccessible"
TELEGRAM_UNEXPECTED_CLIENT_ERROR_DETAIL: Final = "unexpected Telegram client error"

BUILTIN_DOWNLOAD_ERROR_DETAILS: Final = (
    TELEGRAM_NO_RESULT_DETAIL,
    TELEGRAM_MESSAGE_NOT_FOUND_DETAIL,
    TELEGRAM_MESSAGE_NO_MEDIA_DETAIL,
    TELEGRAM_RATE_LIMIT_DETAIL,
    TELEGRAM_CONNECTION_FAILURE_DETAIL,
    TELEGRAM_SOURCE_INACCESSIBLE_DETAIL,
    TELEGRAM_UNEXPECTED_CLIENT_ERROR_DETAIL,
)
PUBLIC_DOWNLOAD_ERROR_DETAILS: Final = frozenset(BUILTIN_DOWNLOAD_ERROR_DETAILS)


def safe_exception_type_name(
    value: BaseException | type[BaseException] | str,
    *,
    fallback: str,
) -> str:
    """Return a bounded exception class name, or ``fallback``.

    ``value`` may be an exception instance, an exception type, or an already
    extracted class name. Only the class name is consulted; exception text,
    cause, and traceback are never read. A name is accepted only when it is a
    bounded ASCII Python identifier, so non-ASCII identifiers and other
    look-alike or confusable text can never reach a log or a stored reason.
    """

    if isinstance(value, str):
        name = value
    elif isinstance(value, type):
        name = value.__name__
    else:
        name = type(value).__name__
    if (
        isinstance(name, str)
        and name.isascii()
        and name.isidentifier()
        and len(name) <= EXCEPTION_TYPE_NAME_MAX_LENGTH
    ):
        return name
    return fallback


def download_error_detail(error: DownloadError) -> str:
    """Return an allowlisted provider detail, or only the exception class name.

    Matching is exact after whitespace normalization. No substring, prefix, or
    regular-expression acceptance is allowed, so custom provider messages cannot
    become user-visible merely by containing a trusted phrase.
    """

    detail = " ".join(str(error).split())
    if detail in PUBLIC_DOWNLOAD_ERROR_DETAILS:
        return detail
    return safe_exception_type_name(error, fallback=DownloadError.__name__)


class JobCancellationRequested(RuntimeError):
    """Internal signal used when a persisted cancellation is observed."""


class PersistenceError(Exception):
    """A persistence failure with fixed, non-sensitive diagnostics.

    Only a stable category, an allowlisted fixed detail, the originating exception
    class name, and an optional integer SQLite primary code or ``errno`` are retained.
    Raw exceptions, messages, paths, SQL, and parameters must never be attached or
    chained, so persistence boundaries raise this type with ``from None``.
    """

    __slots__ = ("category", "code", "detail", "error_type")

    def __init__(
        self,
        category: str,
        detail: str,
        *,
        error_type: str,
        code: int | None = None,
    ) -> None:
        expected_detail = PERSISTENCE_DETAILS.get(category)
        if expected_detail is None:
            raise ValueError("unrecognized persistence category")
        if detail != expected_detail:
            raise ValueError("persistence detail is not allowlisted for the category")
        sanitized_error_type = safe_exception_type_name(
            error_type, fallback=EXCEPTION_TYPE_NAME_FALLBACK
        )
        if not isinstance(error_type, str) or sanitized_error_type != error_type:
            raise ValueError("error_type must be a bounded class name")
        if code is not None and (not isinstance(code, int) or isinstance(code, bool) or code < 0):
            raise ValueError("code must be a non-negative integer")
        super().__init__(f"{category}: {detail}")
        self.category = category
        self.detail = detail
        self.error_type = sanitized_error_type
        self.code = code

    @property
    def retryable(self) -> bool:
        """Whether retrying the same job may succeed after operator recovery."""
        return self.category in TRANSIENT_PERSISTENCE_CATEGORIES
