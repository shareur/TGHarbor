from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from tgharbor.core.job_api import UnauthorizedUserError
from tgharbor.core.jobs import DownloadJob, NewDownloadJob, SourceSession
from tgharbor.core.library_api import LibraryAPI, recover_pending_organizations
from tgharbor.persistence import SQLiteJobQueue
from tgharbor.storage import LocalStorage


async def completed_library_file(
    tmp_path: Path,
) -> tuple[SQLiteJobQueue, LocalStorage, DownloadJob]:
    queue = SQLiteJobQueue(tmp_path / "state/tgharbor.db")
    await queue.initialize()
    storage = LocalStorage(tmp_path / "downloads")
    await storage.initialize()
    pending = await queue.enqueue(
        NewDownloadJob(
            requester_id=1001,
            source_session=SourceSession.BOT,
            source_chat="1001",
            source_message_id=1,
        )
    )
    running = await queue.claim_next("worker")
    assert running is not None and running.id == pending.id
    running = await queue.update_metadata(
        running.id,
        "worker",
        display_title="Shared file",
        original_file_name="clip.mp4",
        mime_type="video/mp4",
        total_bytes=7,
    )
    running = await queue.reserve_name(running.id, "worker", "clip.mp4")
    target = await storage.prepare(running, "clip.mp4")
    target.temporary_path.write_bytes(b"payload")
    output = await storage.commit(target)
    completed = await queue.complete(
        running.id,
        "worker",
        output,
        relative_path=storage.relative_path(output),
    )
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
