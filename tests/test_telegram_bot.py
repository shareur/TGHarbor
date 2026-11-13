from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from tgharbor.core.job_api import JobAPI
from tgharbor.core.jobs import NewDownloadJob, SourceSession
from tgharbor.core.library_api import LibraryAPI
from tgharbor.persistence import SQLiteJobQueue
from tgharbor.storage import LocalStorage
from tgharbor.telegram.bot import TelegramBotController, TelegramJobNotifier


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, object | None]] = []

    def add_event_handler(self, callback: object, event: object) -> None:
        pass

    def remove_event_handler(self, callback: object, event: object) -> None:
        pass

    async def send_message(
        self, actor_id: int, text: str, *, buttons: object | None = None
    ) -> None:
        self.sent.append((actor_id, text, buttons))


class FakeEvent:
    def __init__(
        self,
        *,
        sender_id: int,
        text: str = "",
        media: object | None = None,
        file: object | None = None,
        chat_id: int = 1001,
        message_id: int = 10,
    ) -> None:
        self.sender_id = sender_id
        self.raw_text = text
        self.message = SimpleNamespace(media=media, file=file)
        self.chat_id = chat_id
        self.id = message_id
        self.responses: list[str] = []
        self.buttons: list[object | None] = []

    async def respond(self, text: str, *, buttons: object | None = None) -> None:
        self.responses.append(text)
        self.buttons.append(buttons)


class FakeCallback:
    def __init__(self, sender_id: int, data: bytes) -> None:
        self.sender_id = sender_id
        self.data = data
        self.answers: list[str] = []
        self.responses: list[str] = []
        self.buttons: list[object | None] = []

    async def answer(self, text: str) -> None:
        self.answers.append(text)

    async def respond(self, text: str, *, buttons: object | None = None) -> None:
        self.responses.append(text)
        self.buttons.append(buttons)


def button_data(button: object) -> bytes:
    return button.type.data  # type: ignore[attr-defined]


async def controller(tmp_path: Path) -> tuple[TelegramBotController, SQLiteJobQueue]:
    queue = SQLiteJobQueue(tmp_path / "jobs.db")
    await queue.initialize()
    api = JobAPI(queue, (1001,), max_attempts=3)
    return TelegramBotController(FakeClient(), api), queue


async def full_controller(
    tmp_path: Path,
) -> tuple[TelegramBotController, SQLiteJobQueue, LocalStorage, FakeClient]:
    queue = SQLiteJobQueue(tmp_path / "jobs.db")
    await queue.initialize()
    storage = LocalStorage(tmp_path / "downloads")
    await storage.initialize()
    jobs = JobAPI(queue, (1001, 2002), max_attempts=3)
    library = LibraryAPI(queue, storage, (1001, 2002))
    client = FakeClient()
    return TelegramBotController(client, jobs, library, queue), queue, storage, client


async def completed_file(
    queue: SQLiteJobQueue,
    storage: LocalStorage,
    *,
    message_id: int = 500,
):
    pending = await queue.enqueue(
        NewDownloadJob(
            requester_id=1001,
            source_session=SourceSession.BOT,
            source_chat="1001",
            source_message_id=message_id,
        )
    )
    running = await queue.claim_next("worker")
    assert running is not None and running.id == pending.id
    running = await queue.update_metadata(
        running.id,
        "worker",
        display_title="Readable video title",
        original_file_name="clip.mp4",
        mime_type="video/mp4",
        total_bytes=2048,
    )
    running = await queue.reserve_name(running.id, "worker", "clip.mp4")
    target = await storage.prepare(running, "clip.mp4")
    target.temporary_path.write_bytes(b"x" * 2048)
    output = await storage.commit(target)
    return await queue.complete(
        running.id,
        "worker",
        output,
        relative_path=storage.relative_path(output),
    )


async def test_unauthorized_user_cannot_submit(tmp_path: Path) -> None:
    bot, queue = await controller(tmp_path)
    event = FakeEvent(sender_id=9999, media=object())

    await bot.handle_event(event)

    assert event.responses == ["Unauthorized."]
    assert await queue.claim_next("worker") is None


async def test_media_is_enqueued_for_bot_session(tmp_path: Path) -> None:
    bot, queue = await controller(tmp_path)
    event = FakeEvent(
        sender_id=1001,
        media=object(),
        file=object(),
        chat_id=1001,
        message_id=77,
    )

    await bot.handle_event(event)
    job = await queue.claim_next("worker")

    assert job is not None
    assert job.source_session is SourceSession.BOT
    assert job.source_chat == "1001"
    assert job.source_message_id == 77
    assert "Job accepted" in event.responses[0]


async def test_link_is_enqueued_for_user_session(tmp_path: Path) -> None:
    bot, queue = await controller(tmp_path)
    event = FakeEvent(sender_id=1001, text="https://t.me/example_channel/88")

    await bot.handle_event(event)
    job = await queue.claim_next("worker")

    assert job is not None
    assert job.source_session is SourceSession.USER
    assert job.source_chat == "example_channel"
    assert job.source_message_id == 88


async def test_link_preview_is_enqueued_for_user_session(tmp_path: Path) -> None:
    bot, queue = await controller(tmp_path)
    event = FakeEvent(
        sender_id=1001,
        text="https://t.me/example_channel/88",
        media=object(),
    )

    await bot.handle_event(event)
    job = await queue.claim_next("worker")

    assert job is not None
    assert job.source_session is SourceSession.USER
    assert job.source_chat == "example_channel"
    assert job.source_message_id == 88


async def test_webpage_preview_without_telegram_link_is_rejected(tmp_path: Path) -> None:
    bot, queue = await controller(tmp_path)
    event = FakeEvent(
        sender_id=1001,
        text="https://example.com/video",
        media=object(),
    )

    await bot.handle_event(event)

    assert event.responses[0].startswith("Cannot create job:")
    assert await queue.claim_next("worker") is None


async def test_status_and_cancel_are_owner_scoped(tmp_path: Path) -> None:
    bot, queue = await controller(tmp_path)
    submit = FakeEvent(sender_id=1001, media=object(), file=object(), message_id=99)
    await bot.handle_event(submit)
    job = (await queue.latest_for_requester(1001))[0]

    status = FakeEvent(sender_id=1001, text=f"/status {job.id}")
    await bot.handle_event(status)
    cancel = FakeEvent(sender_id=1001, text=f"/cancel {job.id}")
    await bot.handle_event(cancel)

    assert "pending" in status.responses[0]
    assert "cancelled" in cancel.responses[0]


async def test_status_is_active_only_and_history_uses_readable_metadata(
    tmp_path: Path,
) -> None:
    bot, queue, storage, _ = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    active = FakeEvent(sender_id=1001, media=object(), file=object(), message_id=501)
    await bot.handle_event(active)

    status = FakeEvent(sender_id=1001, text="/status")
    history = FakeEvent(sender_id=1001, text="/history")
    detail = FakeEvent(sender_id=1001, text=f"/status {completed.short_id}")
    await bot.handle_event(status)
    await bot.handle_event(history)
    await bot.handle_event(detail)

    assert "Active jobs" in status.responses[0]
    assert completed.short_id not in status.responses[0]
    assert completed.short_id in history.responses[0]
    assert "Readable video title" in history.responses[0]
    assert "2.0 KiB" in detail.responses[0]


async def test_shared_library_commands_allow_other_authorized_operator(
    tmp_path: Path,
) -> None:
    bot, queue, storage, _ = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)

    files = FakeEvent(sender_id=2002, text="/files")
    stats = FakeEvent(sender_id=2002, text="/stats")
    mkdir = FakeEvent(sender_id=2002, text="/mkdir videos/2026")
    rename = FakeEvent(sender_id=2002, text=f"/rename {completed.short_id} renamed.mp4")
    archive = FakeEvent(sender_id=2002, text=f"/archive {completed.short_id} videos/2026")
    for event in (files, stats, mkdir, rename, archive):
        await bot.handle_event(event)

    stored = await queue.get_file(completed.id)
    assert "Readable video title" in files.responses[0]
    assert "Files: 1" in stats.responses[0]
    assert "archive/videos/2026" in mkdir.responses[0]
    assert "renamed.mp4" in rename.responses[0]
    assert stored is not None
    assert stored.relative_path == Path("archive/videos/2026/renamed.mp4")
    assert stored.last_operator_id == 2002


async def test_persistent_rename_callback_survives_controller_restart(tmp_path: Path) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    await TelegramJobNotifier(client, queue).job_finished(completed)
    buttons = client.sent[0][2]
    assert isinstance(buttons, list)
    rename_data = button_data(buttons[0][1])

    restarted = TelegramBotController(client, bot.jobs, bot.library, queue)
    choose = FakeCallback(1001, rename_data)
    await restarted.handle_callback(choose)
    name = FakeEvent(sender_id=1001, text="after-restart.mp4")
    await restarted.handle_event(name)
    confirmation = name.buttons[0]
    assert isinstance(confirmation, list)
    confirm_data = button_data(confirmation[0][0])
    confirm = FakeCallback(1001, confirm_data)
    await restarted.handle_callback(confirm)

    stored = await queue.get_file(completed.id)
    assert choose.answers == ["Send the new filename."]
    assert "Confirm file organization" in name.responses[0]
    assert stored is not None and stored.current_name == "after-restart.mp4"
    assert stored.last_operator_id == 1001
    assert "Completed" in confirm.answers[0]


async def test_callback_rejects_other_actor_and_expired_session(tmp_path: Path) -> None:
    bot, queue, storage, _ = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    active = await queue.create_interaction(
        1001,
        "rename",
        "ready",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
    )
    expired = await queue.create_interaction(
        1001,
        "archive",
        "ready",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        job_id=completed.id,
    )

    wrong_actor = FakeCallback(2002, f"tg:{active.id}:rename".encode())
    stale = FakeCallback(1001, f"tg:{expired.id}:archive".encode())
    await bot.handle_callback(wrong_actor)
    await bot.handle_callback(stale)

    assert "unauthorized" in wrong_actor.answers[0]
    assert "expired" in stale.answers[0]


async def test_archive_callback_selects_directory_and_invalidates_sibling_buttons(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    await TelegramJobNotifier(client, queue).job_finished(completed)
    actions = client.sent[0][2]
    assert isinstance(actions, list)
    rename_data = button_data(actions[0][1])
    archive_data = button_data(actions[1][0])
    rename_session_id = rename_data.decode().split(":", 2)[1]

    archive = FakeCallback(1001, archive_data)
    await bot.handle_callback(archive)
    directories = archive.buttons[0]
    assert isinstance(directories, list)
    select = FakeCallback(1001, button_data(directories[0][0]))
    await bot.handle_callback(select)
    confirmations = select.buttons[0]
    assert isinstance(confirmations, list)
    confirm = FakeCallback(1001, button_data(confirmations[0][0]))
    await bot.handle_callback(confirm)

    stored = await queue.get_file(completed.id)
    assert stored is not None and stored.relative_path == Path("archive/videos/clip.mp4")
    assert await queue.get_interaction(UUID(rename_session_id)) is None


async def test_persistent_files_and_history_pagination_survives_restart(tmp_path: Path) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    for message_id in range(600, 611):
        await completed_file(queue, storage, message_id=message_id)

    for command, actor_id in (("/files", 2002), ("/history", 1001)):
        first_page = FakeEvent(sender_id=actor_id, text=command)
        await bot.handle_event(first_page)
        buttons = first_page.buttons[0]
        assert isinstance(buttons, list)
        callback_data = button_data(buttons[0][0])
        session_id = UUID(callback_data.decode().split(":", 2)[1])

        restarted = TelegramBotController(client, bot.jobs, bot.library, queue)
        next_page = FakeCallback(actor_id, callback_data)
        await restarted.handle_callback(next_page)

        assert "page 2" in next_page.responses[0]
        assert await queue.get_interaction(session_id) is None

        replay = FakeCallback(actor_id, callback_data)
        await restarted.handle_callback(replay)
        assert "stale" in replay.answers[0]
