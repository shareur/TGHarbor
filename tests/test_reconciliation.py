from __future__ import annotations

from pathlib import Path

import aiosqlite

from tgharbor.core.jobs import JobStatus, NewDownloadJob, SourceSession
from tgharbor.core.reconciliation import LegacyReconciler
from tgharbor.persistence import SQLiteJobQueue
from tgharbor.storage import LocalStorage


async def legacy_job(
    queue: SQLiteJobQueue, storage: LocalStorage, message_id: int, name: str = "clip.mp4"
):
    job = await queue.enqueue(
        NewDownloadJob(
            requester_id=1001,
            source_session=SourceSession.BOT,
            source_chat="1001",
            source_message_id=message_id,
        )
    )
    directory = storage.root / str(job.id)
    directory.mkdir(parents=True)
    output = directory / name
    output.write_bytes(f"payload-{message_id}".encode())
    async with aiosqlite.connect(queue.path) as connection:
        await connection.execute(
            "UPDATE jobs SET status = ?, output_path = ?, total_bytes = ? WHERE id = ?",
            (JobStatus.COMPLETED.value, str(output), output.stat().st_size, str(job.id)),
        )
        await connection.commit()
    completed = await queue.get(job.id)
    assert completed is not None
    return completed, output


async def components(tmp_path: Path) -> tuple[SQLiteJobQueue, LocalStorage]:
    queue = SQLiteJobQueue(tmp_path / "state/tgharbor.db")
    await queue.initialize()
    storage = LocalStorage(tmp_path / "downloads")
    await storage.initialize()
    return queue, storage


async def test_reconciles_completed_legacy_output_into_inbox(tmp_path: Path) -> None:
    queue, storage = await components(tmp_path)
    job, source = await legacy_job(queue, storage, 1)

    result = await LegacyReconciler(queue, storage).run()

    destination = storage.inbox / "clip.mp4"
    assert (result.discovered, result.reconciled, result.failed) == (1, 1, 0)
    assert destination.read_bytes() == b"payload-1"
    assert not source.exists()
    assert not source.parent.exists()
    stored = await queue.get_file(job.id)
    assert stored is not None
    assert stored.relative_path == Path("inbox/clip.mp4")
    assert stored.size_bytes == len(b"payload-1")
    updated = await queue.get(job.id)
    assert updated is not None and updated.output_path == destination
    assert await LegacyReconciler(queue, storage).run() == type(result)(0, 0, 0)


async def test_reconciliation_uses_collision_safe_name_without_overwrite(tmp_path: Path) -> None:
    queue, storage = await components(tmp_path)
    job, _ = await legacy_job(queue, storage, 2)
    existing = storage.inbox / "clip.mp4"
    existing.write_bytes(b"unrelated")

    result = await LegacyReconciler(queue, storage).run()

    destination = storage.inbox / f"clip--{job.short_id}.mp4"
    assert (result.reconciled, result.failed) == (1, 0)
    assert existing.read_bytes() == b"unrelated"
    assert destination.read_bytes() == b"payload-2"
    stored = await queue.get_file(job.id)
    assert stored is not None and stored.relative_path == Path(f"inbox/clip--{job.short_id}.mp4")


async def test_reconciliation_recovers_move_before_database_registration(tmp_path: Path) -> None:
    queue, storage = await components(tmp_path)
    job, source = await legacy_job(queue, storage, 3)
    reserved = await queue.reserve_legacy_name(job.id, "clip.mp4")
    move = await storage.move_legacy_output(reserved)

    assert not source.exists()
    assert move.output_path.read_bytes() == b"payload-3"
    assert move.state_path.exists()
    assert await queue.get_file(job.id) is None

    result = await LegacyReconciler(queue, storage).run()

    assert (result.reconciled, result.failed) == (1, 0)
    assert await queue.get_file(job.id) is not None
    assert not move.state_path.exists()


async def test_reconciliation_leaves_ambiguous_legacy_directory_unchanged(
    tmp_path: Path,
) -> None:
    queue, storage = await components(tmp_path)
    job, source = await legacy_job(queue, storage, 4)
    extra = source.parent / "unexpected.bin"
    extra.write_bytes(b"do-not-touch")

    result = await LegacyReconciler(queue, storage).run()

    assert (result.discovered, result.reconciled, result.failed) == (1, 0, 1)
    assert source.read_bytes() == b"payload-4"
    assert extra.read_bytes() == b"do-not-touch"
    assert await queue.get_file(job.id) is None


async def test_reconciliation_rejects_legacy_output_symlink(tmp_path: Path) -> None:
    queue, storage = await components(tmp_path)
    job = await queue.enqueue(
        NewDownloadJob(
            requester_id=1001,
            source_session=SourceSession.BOT,
            source_chat="1001",
            source_message_id=5,
        )
    )
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    directory = storage.root / str(job.id)
    directory.mkdir()
    output = directory / "clip.mp4"
    output.symlink_to(outside)
    async with aiosqlite.connect(queue.path) as connection:
        await connection.execute(
            "UPDATE jobs SET status = ?, output_path = ? WHERE id = ?",
            (JobStatus.COMPLETED.value, str(output), str(job.id)),
        )
        await connection.commit()

    result = await LegacyReconciler(queue, storage).run()

    assert (result.reconciled, result.failed) == (0, 1)
    assert outside.read_bytes() == b"outside"
    assert output.is_symlink()
    assert await queue.get_file(job.id) is None
