from __future__ import annotations


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


class JobCancellationRequested(RuntimeError):
    """Internal signal used when a persisted cancellation is observed."""
