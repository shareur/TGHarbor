from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from telethon import errors

from tgharbor.core.errors import (
    BUILTIN_DOWNLOAD_ERROR_DETAILS,
    PUBLIC_DOWNLOAD_ERROR_DETAILS,
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
from tgharbor.core.jobs import DownloadJob, JobStatus, SourceSession
from tgharbor.telegram.provider import TelethonMediaProvider

_UNSET = object()


def job(chat: str = "channel", destination_name: str | None = None) -> DownloadJob:
    now = datetime.now(UTC)
    return DownloadJob(
        id=uuid4(),
        requester_id=1001,
        source_session=SourceSession.USER,
        source_chat=chat,
        source_message_id=42,
        destination_name=destination_name,
        status=JobStatus.RUNNING,
        attempt_count=1,
        max_attempts=3,
        progress_bytes=0,
        total_bytes=None,
        output_path=None,
        error=None,
        available_at=now,
        created_at=now,
        updated_at=now,
        claimed_by="worker",
        cancel_requested=False,
    )


class FakeClient:
    def __init__(
        self,
        message: object | None = None,
        error: BaseException | None = None,
        *,
        download_error: BaseException | None = None,
        download_result: object = _UNSET,
    ) -> None:
        self.message = message
        self.error = error
        self.download_error = download_error
        self.download_result = download_result
        self.entity: object | None = None
        self.destination: Path | None = None

    async def get_messages(self, entity: object, *, ids: int) -> object | None:
        self.entity = entity
        if self.error is not None:
            raise self.error
        return self.message

    async def download_media(
        self, message: object, *, file: Path, progress_callback: object
    ) -> object:
        if self.download_error is not None:
            raise self.download_error
        self.destination = file
        file.write_bytes(b"payload")
        await progress_callback(7, 7)
        if self.download_result is _UNSET:
            return file
        return self.download_result


def media_message(
    name: str | None = "clip.mp4",
    *,
    ext: str | None = None,
    mime_type: str | None = None,
    forward: object | None = None,
    chat_id: int | None = None,
    is_channel: bool = False,
    is_group: bool = False,
) -> object:
    return SimpleNamespace(
        media=object(),
        file=SimpleNamespace(name=name, size=7, ext=ext, mime_type=mime_type),
        forward=forward,
        chat_id=chat_id,
        is_channel=is_channel,
        is_group=is_group,
    )


async def _record(updates: list[tuple[int, int | None]], current: int, total: int | None) -> None:
    updates.append((current, total))


async def _ignore(current: int, total: int | None) -> None:
    pass


def test_builtin_detail_constants_exactly_form_public_allowlist() -> None:
    builtin_constants = {
        TELEGRAM_NO_RESULT_DETAIL,
        TELEGRAM_MESSAGE_NOT_FOUND_DETAIL,
        TELEGRAM_MESSAGE_NO_MEDIA_DETAIL,
        TELEGRAM_RATE_LIMIT_DETAIL,
        TELEGRAM_CONNECTION_FAILURE_DETAIL,
        TELEGRAM_SOURCE_INACCESSIBLE_DETAIL,
        TELEGRAM_UNEXPECTED_CLIENT_ERROR_DETAIL,
    }

    assert builtin_constants == PUBLIC_DOWNLOAD_ERROR_DETAILS
    assert frozenset(BUILTIN_DOWNLOAD_ERROR_DETAILS) == builtin_constants
    assert len(BUILTIN_DOWNLOAD_ERROR_DETAILS) == len(builtin_constants)


async def test_describe_and_download_media(tmp_path: Path) -> None:
    client = FakeClient(media_message())
    provider = TelethonMediaProvider(client)
    updates: list[tuple[int, int | None]] = []

    descriptor = await provider.describe(job("-100123"))
    await provider.download(
        job("-100123"),
        tmp_path / "part",
        lambda current, total: _record(updates, current, total),
    )

    assert descriptor.file_name == "clip.mp4"
    assert descriptor.total_bytes == 7
    assert client.entity == -100123
    assert client.destination is not None and client.destination.read_bytes() == b"payload"
    assert updates == [(7, 7)]


async def test_missing_message_is_permanent() -> None:
    client = FakeClient(None)
    provider = TelethonMediaProvider(client)

    with pytest.raises(PermanentDownloadError) as captured:
        await provider.describe(job("channel"))

    assert str(captured.value) == TELEGRAM_MESSAGE_NOT_FOUND_DETAIL
    assert captured.value.retryable is False
    assert client.entity == "channel"


async def test_describe_prefers_explicit_destination_name() -> None:
    provider = TelethonMediaProvider(FakeClient(media_message("clip.mp4")))

    descriptor = await provider.describe(job(destination_name="chosen.jpg"))

    assert descriptor.file_name == "chosen.jpg"


async def test_describe_uses_telethon_extension_for_unnamed_jpeg() -> None:
    message = media_message(None, ext=".jpg", mime_type="image/jpeg")
    provider = TelethonMediaProvider(FakeClient(message))

    descriptor = await provider.describe(job())

    assert descriptor.file_name == "telegram_42.jpg"


async def test_describe_uses_telethon_extension_for_unnamed_mp4() -> None:
    message = media_message(None, ext=".mp4", mime_type="video/mp4")
    provider = TelethonMediaProvider(FakeClient(message))

    descriptor = await provider.describe(job())

    assert descriptor.file_name == "telegram_42.mp4"


async def test_describe_uses_explicit_mime_map_when_extension_is_invalid() -> None:
    message = media_message(None, ext=".PNG", mime_type="image/png")
    provider = TelethonMediaProvider(FakeClient(message))

    descriptor = await provider.describe(job())

    assert descriptor.file_name == "telegram_42.png"


async def test_describe_uses_bin_for_unknown_octet_stream() -> None:
    message = media_message(None, mime_type="application/octet-stream")
    provider = TelethonMediaProvider(FakeClient(message))

    descriptor = await provider.describe(job())

    assert descriptor.file_name == "telegram_42.bin"


async def test_describe_rejects_path_like_extension_and_uses_bin() -> None:
    message = media_message(None, ext="../jpg", mime_type="application/octet-stream")
    provider = TelethonMediaProvider(FakeClient(message))

    descriptor = await provider.describe(job())

    assert descriptor.file_name == "telegram_42.bin"


async def test_missing_media_is_permanent() -> None:
    provider = TelethonMediaProvider(FakeClient(SimpleNamespace(media=None, file=None)))

    with pytest.raises(PermanentDownloadError) as captured:
        await provider.describe(job())

    assert str(captured.value) == TELEGRAM_MESSAGE_NO_MEDIA_DETAIL
    assert captured.value.retryable is False


async def test_webpage_preview_without_file_is_permanent() -> None:
    provider = TelethonMediaProvider(FakeClient(SimpleNamespace(media=object(), file=None)))

    with pytest.raises(PermanentDownloadError) as captured:
        await provider.describe(job())

    assert str(captured.value) == TELEGRAM_MESSAGE_NO_MEDIA_DETAIL


async def test_missing_download_result_is_permanent(tmp_path: Path) -> None:
    provider = TelethonMediaProvider(FakeClient(media_message(), download_result=None))

    with pytest.raises(PermanentDownloadError) as captured:
        await provider.download(job(), tmp_path / "part", _ignore)

    assert str(captured.value) == TELEGRAM_NO_RESULT_DETAIL
    assert captured.value.retryable is False


async def test_flood_wait_is_retryable_with_provider_delay() -> None:
    provider = TelethonMediaProvider(FakeClient(error=errors.FloodWaitError(None, 7)))

    with pytest.raises(RetryableDownloadError) as captured:
        await provider.describe(job())

    assert str(captured.value) == TELEGRAM_RATE_LIMIT_DETAIL
    assert captured.value.retryable is True
    assert captured.value.retry_after_seconds == 7


@pytest.mark.parametrize(
    "error",
    [
        errors.ServerError(None, "secret server detail", 500),
        errors.TimedOutError(None, "secret timeout detail", 504),
        TimeoutError("secret timeout detail"),
    ],
    ids=["server", "timed-out", "timeout"],
)
async def test_retryable_rpc_failures_use_fixed_detail(error: BaseException) -> None:
    provider = TelethonMediaProvider(FakeClient(error=error))

    with pytest.raises(RetryableDownloadError) as captured:
        await provider.describe(job())

    assert str(captured.value) == TELEGRAM_CONNECTION_FAILURE_DETAIL
    assert captured.value.retryable is True
    assert captured.value.retry_after_seconds == 0
    assert "secret" not in str(captured.value)


@pytest.mark.parametrize(
    "error",
    [
        errors.ChannelPrivateError(None),
        errors.MessageIdInvalidError(None),
        errors.UsernameInvalidError(None),
        errors.UsernameNotOccupiedError(None),
        errors.RPCError(None, "secret rpc detail", 500),
    ],
    ids=[
        "channel-private",
        "message-id-invalid",
        "username-invalid",
        "username-not-occupied",
        "rpc",
    ],
)
async def test_permanent_rpc_failures_use_fixed_detail(error: BaseException) -> None:
    provider = TelethonMediaProvider(FakeClient(error=error))

    with pytest.raises(PermanentDownloadError) as captured:
        await provider.describe(job())

    assert str(captured.value) == TELEGRAM_SOURCE_INACCESSIBLE_DETAIL
    assert captured.value.retryable is False
    assert "secret" not in str(captured.value)


async def test_network_failure_is_retryable_without_exposing_message() -> None:
    provider = TelethonMediaProvider(FakeClient(error=OSError("secret transport details")))

    with pytest.raises(RetryableDownloadError) as captured:
        await provider.describe(job())

    assert str(captured.value) == TELEGRAM_CONNECTION_FAILURE_DETAIL
    assert "secret transport details" not in str(captured.value)


async def test_describe_uses_unambiguous_forwarded_channel_metadata() -> None:
    forward = SimpleNamespace(chat_id=-100111, is_channel=True, is_group=False)
    provider = TelethonMediaProvider(FakeClient(media_message(forward=forward)))

    descriptor = await provider.describe(job())

    assert descriptor.origin_group_id == "-100111"
    assert descriptor.channel_id == "-100111"


async def test_describe_uses_group_origin_without_inferring_channel() -> None:
    forward = SimpleNamespace(chat_id=-222, is_channel=False, is_group=True)
    provider = TelethonMediaProvider(FakeClient(media_message(forward=forward)))

    descriptor = await provider.describe(job())

    assert descriptor.origin_group_id == "-222"
    assert descriptor.channel_id is None


async def test_describe_megagroup_forward_is_classified_as_group_first() -> None:
    forward = SimpleNamespace(chat_id=-100777, is_channel=True, is_group=True)
    provider = TelethonMediaProvider(FakeClient(media_message(forward=forward)))

    descriptor = await provider.describe(job())

    assert descriptor.origin_group_id == "-100777"
    assert descriptor.channel_id is None


async def test_describe_direct_megagroup_message_is_classified_as_group_first() -> None:
    message = media_message(chat_id=-100778, is_channel=True, is_group=True)
    provider = TelethonMediaProvider(FakeClient(message))

    descriptor = await provider.describe(job())

    assert descriptor.origin_group_id == "-100778"
    assert descriptor.channel_id is None


async def test_describe_private_forward_without_peer_is_unavailable() -> None:
    forward = SimpleNamespace(chat_id=None, is_channel=False, is_group=False)
    provider = TelethonMediaProvider(FakeClient(media_message(forward=forward)))

    descriptor = await provider.describe(job())

    assert descriptor.origin_group_id is None
    assert descriptor.channel_id is None


async def test_describe_direct_channel_source_and_private_source() -> None:
    channel_provider = TelethonMediaProvider(
        FakeClient(media_message(chat_id=-100999, is_channel=True))
    )
    channel_descriptor = await channel_provider.describe(job())
    assert channel_descriptor.origin_group_id == "-100999"
    assert channel_descriptor.channel_id == "-100999"

    private_provider = TelethonMediaProvider(FakeClient(media_message(chat_id=1001)))
    private_descriptor = await private_provider.describe(job())
    assert private_descriptor.origin_group_id is None
    assert private_descriptor.channel_id is None


async def test_unexpected_client_failure_uses_fixed_allowlisted_detail() -> None:
    provider = TelethonMediaProvider(FakeClient(error=RuntimeError("secret client details")))

    with pytest.raises(RetryableDownloadError) as captured:
        await provider.describe(job())

    assert str(captured.value) == TELEGRAM_UNEXPECTED_CLIENT_ERROR_DETAIL
    assert str(captured.value) in PUBLIC_DOWNLOAD_ERROR_DETAILS
    assert "secret client details" not in str(captured.value)


async def test_cancelled_message_fetch_is_reraised() -> None:
    provider = TelethonMediaProvider(FakeClient(error=asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        await provider.describe(job())


async def test_cancelled_download_is_reraised(tmp_path: Path) -> None:
    provider = TelethonMediaProvider(
        FakeClient(media_message(), download_error=asyncio.CancelledError())
    )

    with pytest.raises(asyncio.CancelledError):
        await provider.download(job(), tmp_path / "part", _ignore)


async def test_download_media_connection_failure_uses_fixed_detail(tmp_path: Path) -> None:
    provider = TelethonMediaProvider(
        FakeClient(media_message(), download_error=OSError("secret transport detail"))
    )

    with pytest.raises(RetryableDownloadError) as captured:
        await provider.download(job(), tmp_path / "part", _ignore)

    assert str(captured.value) == TELEGRAM_CONNECTION_FAILURE_DETAIL
    assert "secret transport detail" not in str(captured.value)


async def test_download_media_unexpected_failure_uses_fixed_detail(tmp_path: Path) -> None:
    provider = TelethonMediaProvider(
        FakeClient(media_message(), download_error=RuntimeError("secret client detail"))
    )

    with pytest.raises(RetryableDownloadError) as captured:
        await provider.download(job(), tmp_path / "part", _ignore)

    assert str(captured.value) == TELEGRAM_UNEXPECTED_CLIENT_ERROR_DETAIL
    assert "secret client detail" not in str(captured.value)
