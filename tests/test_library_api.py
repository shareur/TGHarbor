from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from tgharbor.core.job_api import UnauthorizedUserError
from tgharbor.core.jobs import (
    BatchProgress,
    DownloadJob,
    FileLocation,
    NewDownloadJob,
    SourceSession,
    StoredFile,
)
from tgharbor.core.library_api import (
    BatchMoveCategory,
    BatchNotFoundError,
    LibraryAPI,
    recover_pending_organizations,
)
from tgharbor.persistence import SQLiteJobQueue
from tgharbor.storage import LocalStorage


async def library_components(tmp_path: Path) -> tuple[SQLiteJobQueue, LocalStorage]:
    queue = SQLiteJobQueue(tmp_path / "state/tgharbor.db")
    await queue.initialize()
    storage = LocalStorage(tmp_path / "downloads")
    await storage.initialize()
    return queue, storage


async def add_completed_file(
    queue: SQLiteJobQueue,
    storage: LocalStorage,
    *,
    message_id: int,
    requester_id: int = 1001,
    batch_id: str | None = None,
    batch_expected_size: int = 1,
    file_name: str = "clip.mp4",
    payload: bytes = b"payload",
) -> DownloadJob:
    pending = await queue.enqueue(
        NewDownloadJob(
            requester_id=requester_id,
            source_session=SourceSession.BOT,
            source_chat=str(requester_id),
            source_message_id=message_id,
            batch_id=batch_id,
            batch_expected_size=batch_expected_size,
        )
    )
    running = await queue.claim_next("worker")
    assert running is not None and running.id == pending.id
    running = await queue.update_metadata(
        running.id,
        "worker",
        display_title="Shared file",
        original_file_name=file_name,
        mime_type="video/mp4",
        total_bytes=len(payload),
    )
    running = await queue.reserve_name(running.id, "worker", file_name)
    target = await storage.prepare(running, file_name)
    target.temporary_path.write_bytes(payload)
    output = await storage.commit(target)
    return await queue.complete(
        running.id,
        "worker",
        output,
        relative_path=storage.relative_path(output),
    )


async def completed_library_file(
    tmp_path: Path,
) -> tuple[SQLiteJobQueue, LocalStorage, DownloadJob]:
    queue, storage = await library_components(tmp_path)
    completed = await add_completed_file(queue, storage, message_id=1)
    return queue, storage, completed


async def test_library_requires_authorization_before_shared_access(tmp_path: Path) -> None:
    queue, storage, _ = await completed_library_file(tmp_path)
    library = LibraryAPI(queue, storage, (1001,))

    with pytest.raises(UnauthorizedUserError):
        await library.files(9999)


async def test_startup_recovers_file_move_before_catalog_update(tmp_path: Path) -> None:
    queue, storage, completed = await completed_library_file(tmp_path)
    stored = await queue.get_file(completed.id)
    assert stored is not None

    relative = await storage.organize(
        stored.relative_path,
        stored.job_id.hex,
        new_name="recovered.mp4",
        operator_id=2002,
        action="rename",
    )

    assert relative == Path("inbox/recovered.mp4")
    assert not (storage.root / stored.relative_path).exists()
    assert (storage.root / relative).read_bytes() == b"payload"
    unchanged = await queue.get_file(stored.job_id)
    assert unchanged is not None and unchanged.relative_path == Path("inbox/clip.mp4")
    assert len(await storage.pending_organizations()) == 1

    restarted_storage = LocalStorage(storage.root)
    result = await recover_pending_organizations(queue, restarted_storage)

    assert (result.discovered, result.recovered, result.failed) == (1, 1, 0)
    recovered = await queue.get_file(stored.job_id)
    assert recovered is not None
    assert recovered.relative_path == Path("inbox/recovered.mp4")
    assert recovered.last_operator_id == 2002
    assert await restarted_storage.pending_organizations() == []
    async with aiosqlite.connect(queue.path) as connection:
        audit = await (
            await connection.execute(
                "SELECT operator_id, action, destination_path FROM file_audit WHERE job_id = ?",
                (str(stored.job_id),),
            )
        ).fetchone()
    assert audit == (2002, "rename", "inbox/recovered.mp4")


async def test_archive_directory_recency_uses_audit_and_stored_files(tmp_path: Path) -> None:
    queue, storage, completed = await completed_library_file(tmp_path)
    stored = await queue.get_file(completed.id)
    assert stored is not None
    await storage.create_archive_directory("recent")
    await queue.record_directory_creation(1001, Path("archive/empty"))
    relative = await storage.organize(
        stored.relative_path,
        stored.job_id.hex,
        archive_directory="recent",
        operator_id=1001,
        action="archive",
    )
    await queue.record_file_move(
        stored.job_id,
        1001,
        relative,
        storage.root / relative,
        action="archive",
    )

    recency = await queue.archive_directory_recency()

    assert set(recency) == {"empty", "recent"}
    assert recency["recent"] >= recency["empty"]


class FakeRecencyRepository:
    def __init__(self, values: dict[str, float]) -> None:
        self.values = values

    async def list_files_for_batch(self, requester_id: int, batch_id: str) -> list[StoredFile]:
        return []

    async def batch_progress_for_requester(
        self, requester_id: int, batch_id: str
    ) -> BatchProgress | None:
        return None

    async def archive_directory_recency(self) -> dict[str, float]:
        return self.values


async def test_library_orders_only_existing_directories_by_recent_use(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "downloads")
    await storage.initialize()
    for name in ("beta", "Alpha", "gamma"):
        await storage.create_archive_directory(name)
    library = LibraryAPI(
        FakeRecencyRepository({"beta": 20.0, "gamma": 10.0, "deleted": 99.0}),
        storage,
        (1001,),
    )

    assert await library.directories(1001) == [Path("beta"), Path("gamma"), Path("Alpha")]


async def test_library_orders_recent_ties_and_fallbacks_case_insensitively(
    tmp_path: Path,
) -> None:
    storage = LocalStorage(tmp_path / "downloads")
    await storage.initialize()
    for name in ("Gamma", "Beta", "alpha"):
        await storage.create_archive_directory(name)
    library = LibraryAPI(
        FakeRecencyRepository({"Beta": 10.0, "alpha": 10.0}),
        storage,
        (1001,),
    )

    assert await library.directories(1001) == [Path("alpha"), Path("Beta"), Path("Gamma")]


async def test_batch_file_lookup_is_requester_and_actor_scoped(tmp_path: Path) -> None:
    queue, storage = await library_components(tmp_path)
    await add_completed_file(
        queue,
        storage,
        message_id=10,
        batch_id="batch-auth",
        batch_expected_size=2,
        file_name="first.mp4",
    )
    await add_completed_file(
        queue,
        storage,
        message_id=11,
        batch_id="batch-auth",
        batch_expected_size=2,
        file_name="second.mp4",
    )
    library = LibraryAPI(queue, storage, (1001, 2002))

    files = await library.files_for_batch(1001, "batch-auth")
    assert {stored.current_name for stored in files} == {"first.mp4", "second.mp4"}
    assert await library.files_for_batch(2002, "batch-auth") == []
    with pytest.raises(UnauthorizedUserError):
        await library.files_for_batch(9999, "batch-auth")
    with pytest.raises(BatchNotFoundError):
        await library.organize_batch(2002, "batch-auth", "videos")
    with pytest.raises(UnauthorizedUserError):
        await library.organize_batch(9999, "batch-auth", "videos")


async def test_organize_batch_moves_inbox_members_and_replays_idempotently(
    tmp_path: Path,
) -> None:
    queue, storage = await library_components(tmp_path)
    await storage.create_archive_directory("videos")
    first = await add_completed_file(
        queue,
        storage,
        message_id=12,
        batch_id="batch-move",
        batch_expected_size=2,
        file_name="first.mp4",
    )
    second = await add_completed_file(
        queue,
        storage,
        message_id=13,
        batch_id="batch-move",
        batch_expected_size=2,
        file_name="second.mp4",
    )
    library = LibraryAPI(queue, storage, (1001, 2002))

    result = await library.organize_batch(1001, "batch-move", "videos")

    assert result.total_members == 2
    assert (
        result.moved,
        result.already_moved,
        result.missing,
        result.not_ready,
        result.failed,
    ) == (
        2,
        0,
        0,
        0,
        0,
    )
    assert result.complete
    assert {item.category for item in result.items} == {BatchMoveCategory.MOVED}
    assert str(tmp_path) not in repr(result)
    moved = await library.files_for_batch(1001, "batch-move")
    assert all(stored.location is FileLocation.ARCHIVE for stored in moved)
    assert {stored.relative_path.parent for stored in moved} == {Path("archive/videos")}

    replay = await library.organize_batch(1001, "batch-move", "videos")

    assert (replay.moved, replay.already_moved) == (0, 2)
    assert replay.complete
    assert {item.category for item in replay.items} == {BatchMoveCategory.ALREADY_MOVED}
    async with aiosqlite.connect(queue.path) as connection:
        audit = await (
            await connection.execute(
                "SELECT COUNT(*) FROM file_audit WHERE job_id IN (?, ?)",
                (str(first.id), str(second.id)),
            )
        ).fetchone()
    assert audit == (2,)


async def test_organize_batch_preserves_existing_collision_targets(tmp_path: Path) -> None:
    queue, storage = await library_components(tmp_path)
    completed = await add_completed_file(
        queue,
        storage,
        message_id=14,
        batch_id="batch-collision",
        batch_expected_size=1,
        file_name="clip.mp4",
        payload=b"new payload",
    )
    await storage.create_archive_directory("videos")
    existing = storage.root / "archive/videos/clip.mp4"
    existing.write_bytes(b"keep me")
    library = LibraryAPI(queue, storage, (1001,))

    result = await library.organize_batch(1001, "batch-collision", "videos")

    assert result.moved == 1
    assert result.complete
    assert existing.read_bytes() == b"keep me"
    stored = (await library.files_for_batch(1001, "batch-collision"))[0]
    assert stored.location is FileLocation.ARCHIVE
    assert stored.relative_path.name.startswith(f"clip--{completed.short_id}")
    assert (storage.root / stored.relative_path).read_bytes() == b"new payload"


async def test_organize_batch_reports_partial_storage_failure_without_paths(
    tmp_path: Path,
) -> None:
    queue, storage = await library_components(tmp_path)
    await storage.create_archive_directory("videos")
    first = await add_completed_file(
        queue,
        storage,
        message_id=15,
        batch_id="batch-partial",
        batch_expected_size=2,
        file_name="first.mp4",
    )
    await add_completed_file(
        queue,
        storage,
        message_id=16,
        batch_id="batch-partial",
        batch_expected_size=2,
        file_name="second.mp4",
    )
    first_stored = await queue.get_file(first.id)
    assert first_stored is not None
    (storage.root / first_stored.relative_path).unlink()
    library = LibraryAPI(queue, storage, (1001,))

    result = await library.organize_batch(1001, "batch-partial", "videos")

    assert result.total_members == 2
    assert (result.moved, result.failed) == (1, 1)
    assert not result.complete
    assert {item.category for item in result.items} == {
        BatchMoveCategory.MOVED,
        BatchMoveCategory.FAILED,
    }
    assert str(tmp_path) not in repr(result)


async def test_organize_batch_skips_missing_catalog_and_not_ready_members(
    tmp_path: Path,
) -> None:
    queue, storage = await library_components(tmp_path)
    await storage.create_archive_directory("videos")
    missing = await add_completed_file(
        queue,
        storage,
        message_id=17,
        batch_id="batch-missing",
        batch_expected_size=2,
        file_name="first.mp4",
    )
    await add_completed_file(
        queue,
        storage,
        message_id=18,
        batch_id="batch-missing",
        batch_expected_size=2,
        file_name="second.mp4",
    )
    async with aiosqlite.connect(queue.path) as connection:
        await connection.execute("DELETE FROM stored_files WHERE job_id = ?", (str(missing.id),))
        await connection.commit()
    library = LibraryAPI(queue, storage, (1001,))

    result = await library.organize_batch(1001, "batch-missing", "videos")

    assert (result.moved, result.missing) == (1, 1)
    assert result.not_ready == 0
    assert not result.complete
    assert str(tmp_path) not in repr(result)

    await add_completed_file(
        queue,
        storage,
        message_id=19,
        batch_id="batch-not-ready",
        batch_expected_size=2,
        file_name="ready.mp4",
    )
    pending = await queue.enqueue(
        NewDownloadJob(
            requester_id=1001,
            source_session=SourceSession.BOT,
            source_chat="1001",
            source_message_id=20,
            batch_id="batch-not-ready",
            batch_expected_size=2,
        )
    )
    claimed = await queue.claim_next("worker")
    assert claimed is not None and claimed.id == pending.id
    await queue.fail_or_retry(
        claimed.id,
        "worker",
        "permanent",
        retryable=False,
    )

    partial = await library.organize_batch(1001, "batch-not-ready", "videos")

    assert partial.total_members == 2
    assert (partial.moved, partial.not_ready) == (1, 1)
    assert not partial.complete


async def test_library_repeat_mkdir_and_organize_do_not_duplicate_audit(
    tmp_path: Path,
) -> None:
    queue, storage = await library_components(tmp_path)
    completed = await add_completed_file(queue, storage, message_id=20)
    library = LibraryAPI(queue, storage, (1001,))

    await library.mkdir(1001, "videos")
    await library.mkdir(1001, "videos")
    first = await library.organize(1001, completed.id.hex, archive_directory="videos")
    replay = await library.organize(1001, completed.id.hex, archive_directory="videos")

    assert first.relative_path == Path("archive/videos/clip.mp4")
    assert replay.relative_path == first.relative_path
    async with aiosqlite.connect(queue.path) as connection:
        mkdir_count = await (
            await connection.execute(
                """
                SELECT COUNT(*) FROM file_audit
                WHERE action = 'mkdir' AND destination_path = ?
                """,
                ("archive/videos",),
            )
        ).fetchone()
        move_count = await (
            await connection.execute(
                "SELECT COUNT(*) FROM file_audit WHERE job_id = ?",
                (str(completed.id),),
            )
        ).fetchone()
    assert mkdir_count == (1,)
    assert move_count == (1,)
