from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from telethon import errors

from tgharbor.core.errors import (
    TELEGRAM_CONNECTION_FAILURE_DETAIL,
    TELEGRAM_MESSAGE_NO_MEDIA_DETAIL,
    TELEGRAM_MESSAGE_NOT_FOUND_DETAIL,
    TELEGRAM_NO_RESULT_DETAIL,
    TELEGRAM_RATE_LIMIT_DETAIL,
    TELEGRAM_SOURCE_INACCESSIBLE_DETAIL,
    TELEGRAM_UNEXPECTED_CLIENT_ERROR_DETAIL,
    PermanentDownloadError,
    RetryableDownloadError,
)
from tgharbor.core.interfaces import MediaDescriptor, ProgressCallback
from tgharbor.core.jobs import DownloadJob

_PERMANENT_RPC_ERRORS = (
    errors.ChannelPrivateError,
    errors.MessageIdInvalidError,
    errors.UsernameInvalidError,
    errors.UsernameNotOccupiedError,
)
_RETRYABLE_RPC_ERRORS = (errors.FloodWaitError, errors.ServerError, errors.TimedOutError)
_SAFE_EXTENSION = re.compile(r"\.[a-z0-9]{1,10}\Z")
_MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
}


class TelethonMediaProvider:
    def __init__(self, client: Any) -> None:
        self.client = client

    async def describe(self, job: DownloadJob) -> MediaDescriptor:
        message = await self._message(job)
        telegram_file = getattr(message, "file", None)
        name = _resolve_file_name(job, telegram_file)
        size = getattr(telegram_file, "size", None)
        origin_group_id, channel_id = _source_metadata(message)
        return MediaDescriptor(
            file_name=name,
            total_bytes=size,
            display_title=_display_title(getattr(message, "raw_text", None)),
            mime_type=getattr(telegram_file, "mime_type", None),
            origin_group_id=origin_group_id,
            channel_id=channel_id,
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
            raise PermanentDownloadError(TELEGRAM_NO_RESULT_DETAIL)

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
            raise PermanentDownloadError(TELEGRAM_MESSAGE_NOT_FOUND_DETAIL)
        if getattr(message, "file", None) is None:
            raise PermanentDownloadError(TELEGRAM_MESSAGE_NO_MEDIA_DETAIL)
        return message


def _raise_download_error(error: BaseException) -> None:
    if isinstance(error, errors.FloodWaitError):
        raise RetryableDownloadError(
            TELEGRAM_RATE_LIMIT_DETAIL,
            retry_after_seconds=max(0, float(error.seconds)),
        ) from error
    if isinstance(error, _RETRYABLE_RPC_ERRORS + (OSError, TimeoutError)):
        raise RetryableDownloadError(TELEGRAM_CONNECTION_FAILURE_DETAIL) from error
    if isinstance(error, _PERMANENT_RPC_ERRORS + (errors.RPCError,)):
        raise PermanentDownloadError(TELEGRAM_SOURCE_INACCESSIBLE_DETAIL) from error
    if isinstance(error, asyncio.CancelledError):
        raise error
    raise RetryableDownloadError(TELEGRAM_UNEXPECTED_CLIENT_ERROR_DETAIL) from error


def _source_metadata(message: Any) -> tuple[str | None, str | None]:
    """Return an unambiguous original group/channel peer, never inferred from user IDs.

    A forward with no resolvable chat peer is treated as unavailable rather than
    falling back to the current conversation. A peer flagged as a group is classified
    as a group first, so a megagroup supplies only the original peer and never a
    channel column. A non-group channel supplies both the original peer and the
    channel column. Private users and ambiguous metadata produce ``(None, None)``.
    """

    forward = getattr(message, "forward", None)
    source = message if forward is None else forward
    chat_id = getattr(source, "chat_id", None)
    if chat_id is None:
        return None, None
    if getattr(source, "is_group", False):
        return str(chat_id), None
    if getattr(source, "is_channel", False):
        return str(chat_id), str(chat_id)
    return None, None


def _resolve_file_name(job: DownloadJob, telegram_file: Any) -> str:
    if job.destination_name:
        return job.destination_name
    name = getattr(telegram_file, "name", None)
    if isinstance(name, str) and name.strip():
        return name
    return f"telegram_{job.source_message_id}{_media_extension(telegram_file)}"


def _media_extension(telegram_file: Any) -> str:
    extension = getattr(telegram_file, "ext", None)
    if isinstance(extension, str) and _SAFE_EXTENSION.fullmatch(extension):
        return extension
    mime_type = getattr(telegram_file, "mime_type", None)
    if isinstance(mime_type, str):
        base_type = mime_type.split(";", 1)[0].strip().lower()
        return _MIME_EXTENSIONS.get(base_type, ".bin")
    return ".bin"


def _display_title(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized[:160] or None
