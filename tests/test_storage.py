from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from tgharbor.core.jobs import DownloadJob, JobStatus, SourceSession
from tgharbor.storage import LocalStorage, StorageSafetyError, sanitize_filename


def running_job() -> DownloadJob:
    now = datetime.now(UTC)
    return DownloadJob(
        id=uuid4(),
        requester_id=1001,
        source_session=SourceSession.BOT,
        source_chat="1001",
        source_message_id=1,
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


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        ("../../secret.txt", "_.._secret.txt"),
        ("folder\\video.mp4", "folder_video.mp4"),
        ("\x00\n", "download.bin"),
        (" . ", "download.bin"),
    ],
)
def test_sanitize_filename(original: str, expected: str) -> None:
    assert sanitize_filename(original) == expected


async def test_commit_is_atomic_and_scoped_to_job(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "downloads")
    job = running_job()
    target = await storage.prepare(job, "../clip.mp4")
    target.temporary_path.write_bytes(b"payload")

    completed = await storage.commit(target)

    assert completed == tmp_path / "downloads" / str(job.id) / "_clip.mp4"
    assert completed.read_bytes() == b"payload"
    assert not target.temporary_path.exists()


async def test_prepare_removes_regular_interrupted_file(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "downloads")
    job = running_job()
    target = await storage.prepare(job, "clip.mp4")
    target.temporary_path.write_bytes(b"partial")

    retried = await storage.prepare(job, "clip.mp4")

    assert retried == target
    assert not retried.temporary_path.exists()


async def test_completed_file_survives_database_crash_window(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "downloads")
    job = running_job()
    target = await storage.prepare(job, "clip.mp4")
    target.temporary_path.write_bytes(b"complete")
    await storage.commit(target)

    recovered = await storage.prepare(job, "clip.mp4")

    assert storage.is_complete(recovered)
    assert await storage.commit(recovered) == target.final_path
    assert target.final_path.read_bytes() == b"complete"


async def test_job_directory_symlink_is_rejected(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "downloads")
    await storage.initialize()
    job = running_job()
    outside = tmp_path / "outside"
    outside.mkdir()
    (storage.root / str(job.id)).symlink_to(outside, target_is_directory=True)

    with pytest.raises(StorageSafetyError, match="escapes|real directory"):
        await storage.prepare(job, "clip.mp4")


async def test_temporary_symlink_is_rejected(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "downloads")
    job = running_job()
    target = await storage.prepare(job, "clip.mp4")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    target.temporary_path.symlink_to(outside)

    with pytest.raises(StorageSafetyError, match="regular file|escapes"):
        await storage.prepare(job, "clip.mp4")

    assert outside.read_bytes() == b"outside"


async def test_commit_failure_preserves_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = LocalStorage(tmp_path / "downloads")
    job = running_job()
    target = await storage.prepare(job, "clip.mp4")
    target.temporary_path.write_bytes(b"payload")

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated disk failure"):
        await storage.commit(target)

    assert target.temporary_path.read_bytes() == b"payload"
    assert not target.final_path.exists()
