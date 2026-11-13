from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from telethon import errors

from tgharbor.core.errors import PermanentDownloadError, RetryableDownloadError
from tgharbor.core.interfaces import MediaDescriptor, ProgressCallback
from tgharbor.core.jobs import DownloadJob

_PERMANENT_RPC_ERRORS = (
    errors.ChannelPrivateError,
    errors.MessageIdInvalidError,
    errors.UsernameInvalidError,
    errors.UsernameNotOccupiedError,
)
_RETRYABLE_RPC_ERRORS = (errors.FloodWaitError, errors.ServerError, errors.TimedOutError)


class TelethonMediaProvider:
    def __init__(self, client: Any) -> None:
        self.client = client

    async def describe(self, job: DownloadJob) -> MediaDescriptor:
        message = await self._message(job)
        telegram_file = getattr(message, "file", None)
        name = job.destination_name or getattr(telegram_file, "name", None)
        if not name:
            name = f"telegram_{job.source_message_id}.bin"
        size = getattr(telegram_file, "size", None)
        return MediaDescriptor(
            file_name=name,
            total_bytes=size,
            display_title=_display_title(getattr(message, "raw_text", None)),
            mime_type=getattr(telegram_file, "mime_type", None),
        )

    async def download(
        self, job: DownloadJob, destination: Path, progress: ProgressCallback
    ) -> None:
        message = await self._message(job)
        try:
            result = await self.client.download_media(
                message,
                file=destination,
                progress_callback=progress,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - isolate the external client adapter
            _raise_download_error(error)
        if result is None:
            raise PermanentDownloadError("Telegram returned no downloadable media")

    async def _message(self, job: DownloadJob) -> Any:
        entity: int | str
        try:
            entity = int(job.source_chat)
        except ValueError:
            entity = job.source_chat
        try:
            message = await self.client.get_messages(entity, ids=job.source_message_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - isolate the external client adapter
            _raise_download_error(error)
        if message is None:
            raise PermanentDownloadError("Telegram message was not found or is inaccessible")
        if getattr(message, "file", None) is None:
            raise PermanentDownloadError("Telegram message has no downloadable media")
        return message


def _raise_download_error(error: BaseException) -> None:
    if isinstance(error, errors.FloodWaitError):
        raise RetryableDownloadError(
            "Telegram rate limit",
            retry_after_seconds=max(0, float(error.seconds)),
        ) from error
    if isinstance(error, _RETRYABLE_RPC_ERRORS + (OSError, TimeoutError)):
        raise RetryableDownloadError("temporary Telegram connection failure") from error
    if isinstance(error, _PERMANENT_RPC_ERRORS + (errors.RPCError,)):
        raise PermanentDownloadError("Telegram source is invalid or inaccessible") from error
    if isinstance(error, asyncio.CancelledError):
        raise error
    raise RetryableDownloadError(f"unexpected Telegram {error.__class__.__name__}") from error


def _display_title(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized[:160] or None
