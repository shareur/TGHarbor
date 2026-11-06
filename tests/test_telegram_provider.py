from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tgharbor.core.errors import PermanentDownloadError, RetryableDownloadError
from tgharbor.core.jobs import DownloadJob, JobStatus, SourceSession
from tgharbor.telegram.provider import TelethonMediaProvider


def job(chat: str = "channel") -> DownloadJob:
    now = datetime.now(UTC)
    return DownloadJob(
        id=uuid4(),
        requester_id=1001,
        source_session=SourceSession.USER,
        source_chat=chat,
        source_message_id=42,
        destination_name=None,
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
    def __init__(self, message: object | None = None, error: BaseException | None = None) -> None:
        self.message = message
        self.error = error
        self.entity: object | None = None
        self.destination: Path | None = None

    async def get_messages(self, entity: object, *, ids: int) -> object | None:
        self.entity = entity
        if self.error is not None:
            raise self.error
        return self.message

    async def download_media(
        self, message: object, *, file: Path, progress_callback: object
    ) -> Path:
        self.destination = file
        file.write_bytes(b"payload")
        await progress_callback(7, 7)
        return file


def media_message(name: str | None = "clip.mp4") -> object:
    return SimpleNamespace(
        media=object(),
        file=SimpleNamespace(name=name, size=7),
    )


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


async def _record(updates: list[tuple[int, int | None]], current: int, total: int | None) -> None:
    updates.append((current, total))


async def test_missing_media_is_permanent() -> None:
    provider = TelethonMediaProvider(FakeClient(SimpleNamespace(media=None, file=None)))

    with pytest.raises(PermanentDownloadError, match="no downloadable media"):
        await provider.describe(job())


async def test_webpage_preview_without_file_is_permanent() -> None:
    provider = TelethonMediaProvider(FakeClient(SimpleNamespace(media=object(), file=None)))

    with pytest.raises(PermanentDownloadError, match="no downloadable media"):
        await provider.describe(job())


async def test_network_failure_is_retryable_without_exposing_message() -> None:
    provider = TelethonMediaProvider(FakeClient(error=OSError("secret transport details")))

    with pytest.raises(RetryableDownloadError) as captured:
        await provider.describe(job())

    assert "secret transport details" not in str(captured.value)
