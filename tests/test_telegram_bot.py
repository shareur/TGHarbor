from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import aiosqlite
import pytest
from telethon.errors import (
    MessageIdInvalidError,
    MessageNotModifiedError,
    MessageTooLongError,
)

import tgharbor
from tgharbor.core.job_api import JobAPI
from tgharbor.core.jobs import DownloadJob, JobStatus, NewDownloadJob, SourceSession
from tgharbor.core.library_api import (
    BatchMoveCategory,
    BatchMoveItem,
    BatchMoveResult,
    BatchNotFoundError,
    LibraryAPI,
    StoredFileNotFoundError,
    recover_pending_organizations,
)
from tgharbor.persistence import SQLiteJobQueue
from tgharbor.storage import LocalStorage
from tgharbor.telegram.bot import (
    _BOT_COMMANDS,
    BatchNotificationDispatcher,
    TelegramBotController,
    TelegramJobNotifier,
    _CallbackFeedback,
    _completion_message,
    _DeliveryFailure,
    _format_job,
    register_commands,
)


class FakeClient:
    def __init__(
        self,
        *,
        edit_error: BaseException | None = None,
        send_error: BaseException | None = None,
    ) -> None:
        self.sent: list[tuple[int, str, object | None]] = []
        self.sent_ids: list[int] = []
        self.edited: list[tuple[int, int, str, object | None]] = []
        self.deliveries: list[tuple[str, str, object | None]] = []
        self.edit_error = edit_error
        self.edit_error_sequence: list[BaseException | None] = []
        self.edit_gate_text: str | None = None
        self.edit_gate_entered = asyncio.Event()
        self.edit_gate_release = asyncio.Event()
        self.send_error = send_error
        self._next_message_id = 500
        FakeCallback.client = self

    @property
    def last_text(self) -> str:
        return self.deliveries[-1][1] if self.deliveries else ""

    @property
    def last_buttons(self) -> object | None:
        return self.deliveries[-1][2] if self.deliveries else None

    def add_event_handler(self, callback: object, event: object) -> None:
        pass

    def remove_event_handler(self, callback: object, event: object) -> None:
        pass

    async def send_message(
        self, actor_id: int, text: str, *, buttons: object | None = None
    ) -> SimpleNamespace:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((actor_id, text, buttons))
        self.deliveries.append(("send", text, buttons))
        self._next_message_id += 1
        self.sent_ids.append(self._next_message_id)
        return SimpleNamespace(id=self._next_message_id)

    async def edit_message(
        self,
        actor_id: int,
        message_id: int,
        text: str,
        *,
        buttons: object | None = None,
    ) -> SimpleNamespace:
        if self.edit_error is not None:
            raise self.edit_error
        if self.edit_error_sequence:
            sequence_error = self.edit_error_sequence.pop(0)
            if sequence_error is not None:
                raise sequence_error
        if self.edit_gate_text is not None and self.edit_gate_text in text:
            self.edit_gate_entered.set()
            await self.edit_gate_release.wait()
        self.edited.append((actor_id, message_id, text, buttons))
        self.deliveries.append(("edit", text, buttons))
        return SimpleNamespace(id=message_id)


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
        self.response_ids: list[int] = []
        self._next_response_id = 10_000 + message_id

    async def respond(self, text: str, *, buttons: object | None = None) -> SimpleNamespace:
        self.responses.append(text)
        self.buttons.append(buttons)
        self._next_response_id += 1
        self.response_ids.append(self._next_response_id)
        return SimpleNamespace(id=self._next_response_id)


def last_callback_buttons(callback: FakeCallback) -> object | None:
    return (
        callback.edit_buttons[-1]
        if callback.edit_buttons
        else (callback.buttons[-1] if callback.buttons else None)
    )


class FakeCallback:
    """Callback fake mirroring Telethon 1.45's implicit-answer shorthands.

    ``edit`` and ``respond`` both create an extra answer task in real Telethon.
    Tests assert one acknowledgement, so any production use of those shorthands
    fails even though the client-level APIs keep working.
    """

    client: FakeClient | None = None
    _active: FakeCallback | None = None

    def __init__(
        self,
        sender_id: int | None,
        data: bytes,
        *,
        log: list[tuple[str, str]] | None = None,
        message_id: int = 50,
        edit_error: BaseException | None = None,
        chat_id: int | None = None,
    ) -> None:
        self.sender_id = sender_id
        self.data = data
        self.message_id = message_id
        self.chat_id = (
            (sender_id if sender_id is not None else 1001) if chat_id is None else chat_id
        )
        self.edit_error = edit_error
        self.calls: list[tuple[str, str]] = [] if log is None else log
        self.answers: list[str] = []
        self.implicit_answers = 0
        self._client = self.__class__.client
        self._sent_start = len(self._client.sent) if self._client is not None else 0
        self._edited_start = len(self._client.edited) if self._client is not None else 0
        self._sent_end: int | None = None
        self._edited_end: int | None = None
        self._started = False
        self._manual_responses: list[str] = []
        self._manual_buttons: list[object | None] = []
        self._manual_edits: list[str] = []
        self._manual_edit_buttons: list[object | None] = []

    def _snapshot(self) -> None:
        if self._client is None:
            return
        self._sent_end = len(self._client.sent)
        self._edited_end = len(self._client.edited)

    def _sent_window(self) -> list[tuple[int, str, object | None]]:
        if self._client is None:
            return []
        end = len(self._client.sent) if self._sent_end is None else self._sent_end
        return self._client.sent[self._sent_start : end]

    def _edited_window(self) -> list[tuple[int, int, str, object | None]]:
        if self._client is None:
            return []
        end = len(self._client.edited) if self._edited_end is None else self._edited_end
        return self._client.edited[self._edited_start : end]

    async def answer(self, text: str = "") -> None:
        if not self._started:
            self._started = True
            if self._client is not None:
                self._sent_start = len(self._client.sent)
                self._edited_start = len(self._client.edited)
            active = self.__class__._active
            if active is not None and active is not self:
                active._snapshot()
            self.__class__._active = self
        self.answers.append(text)
        self.calls.append(("answer", text))

    async def respond(self, text: str, *, buttons: object | None = None) -> SimpleNamespace:
        self.implicit_answers += 1
        await self.answer()
        self._manual_responses.append(text)
        self._manual_buttons.append(buttons)
        self.calls.append(("respond", text))
        return SimpleNamespace(id=self.message_id + 10_000)

    async def edit(self, text: str, *, buttons: object | None = None) -> SimpleNamespace:
        self.implicit_answers += 1
        await self.answer()
        if self.edit_error is not None:
            raise self.edit_error
        self._manual_edits.append(text)
        self._manual_edit_buttons.append(buttons)
        self.calls.append(("edit", text))
        return SimpleNamespace(id=self.message_id)

    @property
    def edits(self) -> list[str]:
        if self._client is None:
            return list(self._manual_edits)
        observed = [
            text
            for _, message_id, text, _ in self._edited_window()
            if message_id == self.message_id
        ]
        return self._manual_edits + observed

    @property
    def edit_buttons(self) -> list[object | None]:
        if self._client is None:
            return list(self._manual_edit_buttons)
        observed = [
            buttons
            for _, message_id, _, buttons in self._edited_window()
            if message_id == self.message_id
        ]
        return self._manual_edit_buttons + observed

    @property
    def responses(self) -> list[str]:
        if self._client is None:
            return list(self._manual_responses)
        observed = [text for actor_id, text, _ in self._sent_window() if actor_id == self.chat_id]
        return self._manual_responses + observed

    @property
    def buttons(self) -> list[object | None]:
        if self._client is None:
            return list(self._manual_buttons)
        observed = [
            buttons for actor_id, _, buttons in self._sent_window() if actor_id == self.chat_id
        ]
        return self._manual_buttons + observed

    @property
    def last_text(self) -> str:
        if self.edits:
            return self.edits[-1]
        return self.responses[-1] if self.responses else ""

    @property
    def last_buttons(self) -> object | None:
        if self.edit_buttons:
            return self.edit_buttons[-1]
        return self.buttons[-1] if self.buttons else None


def button_data(button: object) -> bytes:
    return button.type.data  # type: ignore[attr-defined]


def record_method(target: object, name: str, log: list[tuple[str, str]]) -> None:
    original = getattr(target, name)

    async def wrapper(*args: object, **kwargs: object) -> object:
        log.append(("dependency", name))
        return await original(*args, **kwargs)

    setattr(target, name, wrapper)


def assert_acknowledged_first(log: list[tuple[str, str]]) -> None:
    assert log[0] == ("answer", "")
    assert sum(1 for kind, _ in log if kind == "answer") == 1


async def controller(tmp_path: Path) -> tuple[TelegramBotController, SQLiteJobQueue]:
    queue = SQLiteJobQueue(tmp_path / "jobs.db")
    await queue.initialize()
    api = JobAPI(queue, (1001,), max_attempts=3)
    return TelegramBotController(FakeClient(), api), queue


async def full_controller(
    tmp_path: Path,
    *,
    batch_identity: str = "telegram-bot",
    wake_workers: object | None = None,
) -> tuple[TelegramBotController, SQLiteJobQueue, LocalStorage, FakeClient]:
    queue = SQLiteJobQueue(tmp_path / "jobs.db")
    await queue.initialize()
    storage = LocalStorage(tmp_path / "downloads")
    await storage.initialize()
    jobs = JobAPI(queue, (1001, 2002), max_attempts=3, wake_workers=wake_workers)  # type: ignore[arg-type]
    library = LibraryAPI(queue, storage, (1001, 2002))
    client = FakeClient()
    return (
        TelegramBotController(
            client,
            jobs,
            library,
            queue,
            batch_identity=batch_identity,
        ),
        queue,
        storage,
        client,
    )


async def completed_file(
    queue: SQLiteJobQueue,
    storage: LocalStorage,
    *,
    message_id: int = 500,
    batch_id: str | None = None,
    batch_expected_size: int = 1,
    origin_group_id: str | None = None,
    channel_id: str | None = None,
    started_at: float = 1000,
    finished_at: float = 1010,
    total_bytes: int = 2048,
):
    pending = await queue.enqueue(
        NewDownloadJob(
            requester_id=1001,
            source_session=SourceSession.BOT,
            source_chat="1001",
            source_message_id=message_id,
            batch_id=batch_id,
            batch_expected_size=batch_expected_size,
            origin_group_id=origin_group_id,
            channel_id=channel_id,
            created_at=datetime.fromtimestamp(started_at - 10, UTC),
        )
    )
    running = await queue.claim_next("worker", now=started_at)
    assert running is not None and running.id == pending.id
    running = await queue.update_metadata(
        running.id,
        "worker",
        display_title="Readable video title",
        original_file_name="clip.mp4",
        mime_type="video/mp4",
        total_bytes=total_bytes,
    )
    running = await queue.reserve_name(running.id, "worker", "clip.mp4")
    target = await storage.prepare(running, "clip.mp4")
    target.temporary_path.write_bytes(b"x" * total_bytes)
    output = await storage.commit(target)
    return await queue.mark_completed(
        running.id,
        "worker",
        output,
        relative_path=storage.relative_path(output),
        now=finished_at,
    )


async def batch_notice_state(queue: SQLiteJobQueue, batch_id: str) -> str | None:
    async with aiosqlite.connect(queue.path) as connection:
        cursor = await connection.execute(
            "SELECT state FROM batch_notifications WHERE requester_id = ? AND batch_id = ?",
            (1001, batch_id),
        )
        row = await cursor.fetchone()
    return None if row is None else str(row[0])


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
    assert "Download queued" in event.responses[0]
    assert "Next: /status" in event.responses[0]


async def test_link_is_enqueued_for_user_session(tmp_path: Path) -> None:
    bot, queue = await controller(tmp_path)
    event = FakeEvent(sender_id=1001, text="https://t.me/example_channel/88")

    await bot.handle_event(event)
    job = await queue.claim_next("worker")

    assert job is not None
    assert job.source_session is SourceSession.USER
    assert job.source_chat == "example_channel"
    assert job.source_message_id == 88
    assert job.batch_id is None
    assert job.batch_expected_size == 1


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
    confirmation = client.last_buttons
    assert isinstance(confirmation, list)
    confirm_data = button_data(confirmation[0][0])
    confirm = FakeCallback(1001, confirm_data)
    await restarted.handle_callback(confirm)

    stored = await queue.get_file(completed.id)
    assert choose.answers == [""]
    assert "Send the new filename." in client.edited[0][2]
    assert any("Confirm file organization" in edit[2] for edit in client.edited)
    assert stored is not None and stored.current_name == "after-restart.mp4"
    assert stored.last_operator_id == 1001
    assert confirm.answers == [""]
    assert "Completed" in confirm.last_text


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

    assert_acknowledged_first(wrong_actor.calls)
    assert "unauthorized" in wrong_actor.edits[-1]
    assert_acknowledged_first(stale.calls)
    assert "expired" in stale.edits[-1]


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
    directories = archive.edit_buttons[0]
    assert isinstance(directories, list)
    select = FakeCallback(1001, button_data(directories[0][0]))
    await bot.handle_callback(select)
    confirmations = select.edit_buttons[0]
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
        next_page = FakeCallback(
            actor_id,
            callback_data,
            message_id=first_page.response_ids[-1],
        )
        await restarted.handle_callback(next_page)

        assert "page 2" in next_page.edits[0]
        assert next_page.responses == []
        assert next_page.answers == [""]
        assert await queue.get_interaction(session_id) is None
        page_action = "files-page" if command == "/files" else "history-page"
        async with aiosqlite.connect(queue.path) as connection:
            bound = await (
                await connection.execute(
                    "SELECT prompt_message_id FROM interactions WHERE action = ?",
                    (page_action,),
                )
            ).fetchall()
        assert [row[0] for row in bound] == [first_page.response_ids[-1]]

        replay = FakeCallback(actor_id, callback_data)
        await restarted.handle_callback(replay)
        assert replay.answers == [""]
        assert "stale" in replay.responses[0]


async def test_callback_acknowledges_first_and_persists_keep_result(tmp_path: Path) -> None:
    bot, queue, storage, _ = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    session = await queue.create_interaction(
        1001,
        "keep",
        "ready",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
    )
    log: list[tuple[str, str]] = []
    record_method(queue, "get_interaction", log)
    record_method(queue, "delete_interactions_for_job", log)
    event = FakeCallback(1001, f"tg:{session.id}:keep".encode(), log=log)

    await bot.handle_callback(event)

    assert_acknowledged_first(log)
    assert "Kept in Inbox" in event.last_text
    assert "Download Completed" in event.last_text


async def test_invalid_and_missing_callbacks_acknowledge_before_persistent_errors(
    tmp_path: Path,
) -> None:
    bot, queue, storage, _ = await full_controller(tmp_path)
    await completed_file(queue, storage)
    invalid_log: list[tuple[str, str]] = []
    invalid = FakeCallback(1001, b"not-a-callback", log=invalid_log)
    await bot.handle_callback(invalid)
    assert_acknowledged_first(invalid_log)
    assert "invalid or unavailable" in invalid.responses[0]

    missing_log: list[tuple[str, str]] = []
    record_method(queue, "get_interaction", missing_log)
    missing = FakeCallback(1001, f"tg:{uuid4()}:keep".encode(), log=missing_log)
    await bot.handle_callback(missing)
    assert_acknowledged_first(missing_log)
    assert "stale" in missing.responses[0]


async def test_directory_selection_acknowledges_before_library_query(tmp_path: Path) -> None:
    bot, queue, storage, _ = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    session = await queue.create_interaction(
        1001,
        "archive",
        "ready",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
    )
    prompt = FakeCallback(1001, f"tg:{session.id}:archive".encode())
    await bot.handle_callback(prompt)
    directories = prompt.edit_buttons[0]
    assert isinstance(directories, list)
    assert [row[0].text for row in directories] == ["videos", "New directory", "Cancel"]
    log: list[tuple[str, str]] = []
    record_method(queue, "get_interaction", log)
    record_method(bot.library, "directories", log)
    event = FakeCallback(1001, button_data(directories[0][0]), log=log)

    await bot.handle_callback(event)

    assert_acknowledged_first(log)
    assert ("dependency", "directories") in log
    assert "Confirm file organization" in event.last_text
    assert event.edit_buttons[0] is not None


async def test_directory_selection_binds_digest_and_rejects_reordering(
    tmp_path: Path,
) -> None:
    bot, queue, storage, _ = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    assert bot.library is not None
    await bot.library.mkdir(1001, "older")
    await bot.library.mkdir(1001, "recent")
    async with aiosqlite.connect(queue.path) as connection:
        await connection.execute(
            "UPDATE file_audit SET created_at = ? WHERE destination_path = ?",
            (10.0, "archive/older"),
        )
        await connection.execute(
            "UPDATE file_audit SET created_at = ? WHERE destination_path = ?",
            (20.0, "archive/recent"),
        )
        await connection.commit()
    session = await queue.create_interaction(
        1001,
        "archive",
        "ready",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
    )
    prompt = FakeCallback(1001, f"tg:{session.id}:archive".encode())
    await bot.handle_callback(prompt)
    rendered = prompt.edit_buttons[0]
    assert isinstance(rendered, list)
    assert [row[0].text for row in rendered] == ["recent", "older", "New directory", "Cancel"]
    stale_payload = button_data(rendered[0][0])
    assert len(stale_payload) <= 64
    assert b"recent" not in stale_payload
    assert b"archive" not in stale_payload
    before = await queue.get_file(completed.id)
    assert before is not None

    async with aiosqlite.connect(queue.path) as connection:
        await connection.execute(
            "UPDATE file_audit SET created_at = ? WHERE destination_path = ?",
            (30.0, "archive/older"),
        )
        await connection.commit()

    stale_click = FakeCallback(1001, stale_payload)
    await bot.handle_callback(stale_click)

    assert "stale" in stale_click.last_text
    assert "Confirm file organization" not in stale_click.last_text
    refreshed = stale_click.edit_buttons[0]
    assert isinstance(refreshed, list)
    assert [row[0].text for row in refreshed] == ["older", "recent", "New directory", "Cancel"]
    unchanged = await queue.get_interaction(session.id)
    assert unchanged is not None
    assert unchanged.step == "await-directory"
    assert unchanged.pending_directory is None
    stored = await queue.get_file(completed.id)
    assert stored is not None
    assert stored.relative_path == before.relative_path

    select = FakeCallback(1001, button_data(refreshed[1][0]))
    await bot.handle_callback(select)
    assert "Confirm file organization" in select.last_text
    assert "Directory: recent" in select.last_text
    confirmation = select.edit_buttons[0]
    assert isinstance(confirmation, list)
    confirm = FakeCallback(1001, button_data(confirmation[0][0]))
    await bot.handle_callback(confirm)

    moved = await queue.get_file(completed.id)
    assert moved is not None
    assert moved.relative_path.parent == Path("archive/recent")


async def test_expired_callback_acknowledges_before_repository_lookup(tmp_path: Path) -> None:
    bot, queue, storage, _ = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    session = await queue.create_interaction(
        1001,
        "keep",
        "ready",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        job_id=completed.id,
    )
    log: list[tuple[str, str]] = []
    record_method(queue, "get_interaction", log)
    event = FakeCallback(1001, f"tg:{session.id}:keep".encode(), log=log)

    await bot.handle_callback(event)

    assert_acknowledged_first(log)
    assert ("dependency", "get_interaction") in log
    assert "expired" in event.edits[-1]


async def test_unauthorized_callback_acknowledges_first_without_repository_lookup(
    tmp_path: Path,
) -> None:
    bot, queue, storage, _ = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    session = await queue.create_interaction(
        1001,
        "keep",
        "ready",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
    )
    log: list[tuple[str, str]] = []
    record_method(queue, "get_interaction", log)
    event = FakeCallback(9999, f"tg:{session.id}:keep".encode(), log=log)

    await bot.handle_callback(event)

    assert_acknowledged_first(log)
    assert ("dependency", "get_interaction") not in log
    assert event.edits == ["Unauthorized."]


async def test_new_directory_callback_persists_creation_prompt(tmp_path: Path) -> None:
    bot, queue, storage, _ = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    session = await queue.create_interaction(
        1001,
        "archive",
        "await-directory",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
    )
    log: list[tuple[str, str]] = []
    record_method(queue, "get_interaction", log)
    record_method(queue, "activate_text_workflow", log)
    event = FakeCallback(1001, f"tg:{session.id}:new-directory".encode(), log=log)

    await bot.handle_callback(event)

    assert_acknowledged_first(log)
    assert ("dependency", "activate_text_workflow") in log
    assert "Send a relative path, for example: videos/2026. Do not start with /." in event.last_text


async def test_pagination_callback_acknowledges_first_and_renders_page(tmp_path: Path) -> None:
    bot, queue, storage, _ = await full_controller(tmp_path)
    await completed_file(queue, storage)
    session = await queue.create_interaction(
        1001,
        "files-page",
        "2",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    log: list[tuple[str, str]] = []
    record_method(queue, "get_interaction", log)
    record_method(queue, "delete_interaction", log)
    event = FakeCallback(1001, f"tg:{session.id}:files-page".encode(), log=log)

    await bot.handle_callback(event)

    assert_acknowledged_first(log)
    assert "page 2" in event.edits[0]
    assert event.responses == []


async def interaction_rows(queue: SQLiteJobQueue) -> list[tuple[object, ...]]:
    async with aiosqlite.connect(queue.path) as connection:
        return await (
            await connection.execute("SELECT id, prompt_message_id FROM interactions")
        ).fetchall()


async def test_page_callback_not_modified_is_success_without_duplicate(tmp_path: Path) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    await completed_file(queue, storage)
    session = await queue.create_interaction(
        1001,
        "files-page",
        "2",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    client.edit_error = MessageNotModifiedError(None)
    event = FakeCallback(
        1001,
        f"tg:{session.id}:files-page".encode(),
        message_id=88,
    )

    await bot.handle_callback(event)

    assert event.answers == [""]
    assert event.edits == []
    assert event.responses == []
    assert await queue.get_interaction(session.id) is None
    rows = await interaction_rows(queue)
    assert [row[1] for row in rows] == [88]


async def test_page_callback_edit_failure_falls_back_once_with_valid_buttons(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    for message_id in range(700, 711):
        await completed_file(queue, storage, message_id=message_id)
    session = await queue.create_interaction(
        1001,
        "files-page",
        "2",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    client.edit_error = MessageIdInvalidError(None)
    event = FakeCallback(
        1001,
        f"tg:{session.id}:files-page".encode(),
        message_id=90,
    )
    caplog.set_level(logging.WARNING, logger="tgharbor.telegram.bot")

    await bot.handle_callback(event)

    assert event.answers == [""]
    assert event.edits == []
    assert len(event.responses) == 1
    assert "page 2" in event.responses[0]
    assert event.buttons[0] is not None
    replacement_id = client.sent_ids[-1]
    assert replacement_id != 90
    page_button = event.buttons[0][0][0]
    page_session = await queue.get_interaction(
        UUID(button_data(page_button).decode().split(":", 2)[1])
    )
    assert page_session is not None
    assert page_session.step == "1"
    assert page_session.prompt_message_id == replacement_id
    assert "outcome=fallback" in caplog.text
    assert "error_type=MessageIdInvalidError" in caplog.text
    assert session.id.hex not in caplog.text
    assert "tg:" not in caplog.text


async def test_page_callback_replay_does_not_mutate_or_edit_again(tmp_path: Path) -> None:
    bot, queue, storage, _ = await full_controller(tmp_path)
    await completed_file(queue, storage)
    session = await queue.create_interaction(
        1001,
        "history-page",
        "2",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    data = f"tg:{session.id}:history-page".encode()
    first = FakeCallback(1001, data, message_id=91)

    await bot.handle_callback(first)
    assert len(first.edits) == 1
    rows_after_first = await interaction_rows(queue)
    assert len(rows_after_first) == 1

    replay = FakeCallback(1001, data)
    await bot.handle_callback(replay)
    assert replay.answers == [""]
    assert "stale" in replay.responses[0]
    assert replay.edits == []
    assert await interaction_rows(queue) == rows_after_first

    cross_actor = FakeCallback(2002, data)
    await bot.handle_callback(cross_actor)
    assert cross_actor.answers == [""]
    assert "unauthorized" in cross_actor.responses[0]
    assert await interaction_rows(queue) == rows_after_first


async def test_page_next_and_previous_edit_the_originating_message(tmp_path: Path) -> None:
    bot, queue, storage, _ = await full_controller(tmp_path)
    for message_id in range(720, 731):
        await completed_file(queue, storage, message_id=message_id)

    for command, actor_id in (("/files", 2002), ("/history", 1001)):
        first = FakeEvent(sender_id=actor_id, text=command)
        await bot.handle_event(first)
        next_button = first.buttons[0][0][0]
        message_id = first.response_ids[-1]

        log: list[tuple[str, str]] = []
        next_page = FakeCallback(
            actor_id,
            button_data(next_button),
            log=log,
            message_id=message_id,
        )
        await bot.handle_callback(next_page)
        assert_acknowledged_first(log)
        assert "page 2" in next_page.edits[0]
        assert next_page.responses == []

        previous_button = next_page.edit_buttons[0][0][0]
        previous_log: list[tuple[str, str]] = []
        previous_page = FakeCallback(
            actor_id,
            button_data(previous_button),
            log=previous_log,
            message_id=message_id,
        )
        await bot.handle_callback(previous_page)
        assert_acknowledged_first(previous_log)
        assert "page 1" in previous_page.edits[0]
        assert previous_page.responses == []


async def test_text_input_edits_the_stored_prompt_anchor(tmp_path: Path) -> None:
    bot, queue, _storage, client = await full_controller(tmp_path)
    session = await queue.create_interaction(
        1001,
        "rename",
        "await-name",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        prompt_message_id=321,
    )

    event = FakeEvent(sender_id=1001, text="kept-name.mp4")
    await bot.handle_event(event)

    assert event.responses == []
    assert len(client.edited) == 1
    actor_id, message_id, text, buttons = client.edited[0]
    assert (actor_id, message_id) == (1001, 321)
    assert "Confirm file organization" in text
    assert "kept-name.mp4" in text
    assert buttons is not None
    refreshed = await queue.get_interaction(session.id)
    assert refreshed is not None
    assert refreshed.step == "confirm"
    assert refreshed.prompt_message_id == 321

    again = FakeEvent(sender_id=1001, text="still waiting")
    await bot.handle_event(again)
    assert client.edited[1][1] == 321
    assert again.responses == []


async def test_prompt_edit_failure_falls_back_once_and_rebinds(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot, queue, _storage, client = await full_controller(tmp_path)
    client.edit_error = MessageIdInvalidError(None)
    session = await queue.create_interaction(
        1001,
        "rename",
        "await-name",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        prompt_message_id=400,
    )
    caplog.set_level(logging.WARNING, logger="tgharbor.telegram.bot")

    event = FakeEvent(sender_id=1001, text="kept-name.mp4")
    await bot.handle_event(event)

    assert event.responses == []
    assert client.edited == []
    assert len(client.sent) == 1
    assert "Confirm file organization" in client.sent[0][1]
    replacement_id = client.sent_ids[-1]
    refreshed = await queue.get_interaction(session.id)
    assert refreshed is not None
    assert refreshed.prompt_message_id == replacement_id
    assert "outcome=fallback" in caplog.text
    assert "error_type=MessageIdInvalidError" in caplog.text
    assert "secret /srv/path" not in caplog.text
    assert session.id.hex not in caplog.text
    assert "kept-name.mp4" not in caplog.text


async def test_prompt_not_modified_is_success_without_duplicate(tmp_path: Path) -> None:
    bot, queue, _storage, client = await full_controller(tmp_path)
    client.edit_error = MessageNotModifiedError(None)
    session = await queue.create_interaction(
        1001,
        "rename",
        "await-name",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        prompt_message_id=400,
    )

    event = FakeEvent(sender_id=1001, text="kept-name.mp4")
    await bot.handle_event(event)

    assert event.responses == []
    assert client.sent == []
    assert client.edited == []
    refreshed = await queue.get_interaction(session.id)
    assert refreshed is not None
    assert refreshed.prompt_message_id == 400


async def test_callback_edit_cancellation_is_reraised_after_acknowledgement(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    await completed_file(queue, storage)
    session = await queue.create_interaction(
        1001,
        "files-page",
        "2",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    client.edit_error = asyncio.CancelledError()
    event = FakeCallback(
        1001,
        f"tg:{session.id}:files-page".encode(),
    )

    with pytest.raises(asyncio.CancelledError):
        await bot.handle_callback(event)

    assert event.answers == [""]


async def test_callback_deliver_falls_back_once_for_known_edit_errors() -> None:
    client = FakeClient(edit_error=MessageIdInvalidError(None))
    event = FakeCallback(1001, b"", message_id=55)
    feedback = _CallbackFeedback(event, client)

    message_id = await feedback.deliver("Replacement text", buttons=None)

    assert message_id == client.sent_ids[-1]
    assert message_id != 55
    assert event.edits == []
    assert event.responses == ["Replacement text"]
    assert event.implicit_answers == 0


async def test_domain_error_acknowledges_first_and_persists_failure(tmp_path: Path) -> None:
    bot, queue, storage, _ = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    assert bot.library is not None
    session = await queue.create_interaction(
        1001,
        "rename",
        "confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
        pending_name="invalid/name.mp4",
    )
    log: list[tuple[str, str]] = []

    async def fail_organize(*args: object, **kwargs: object) -> object:
        log.append(("dependency", "organize"))
        raise ValueError("invalid destination name")

    record_method(queue, "get_interaction", log)
    bot.library.organize = fail_organize  # type: ignore[method-assign]
    event = FakeCallback(1001, f"tg:{session.id}:confirm".encode(), log=log)

    await bot.handle_callback(event)

    assert_acknowledged_first(log)
    assert ("dependency", "organize") in log
    assert "The name or directory is invalid. Review the values and try again." in event.last_text


async def test_unexpected_callback_error_acknowledges_first_and_logs_safely(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot, queue, storage, _ = await full_controller(tmp_path)
    await completed_file(queue, storage)
    log: list[tuple[str, str]] = []

    async def fail_lookup(*args: object, **kwargs: object) -> object:
        log.append(("dependency", "get_interaction"))
        raise RuntimeError("private database detail")

    queue.get_interaction = fail_lookup  # type: ignore[method-assign]
    caplog.set_level(logging.ERROR, logger="tgharbor.telegram.bot")
    event = FakeCallback(1001, f"tg:{uuid4()}:keep".encode(), log=log)

    await bot.handle_callback(event)

    assert_acknowledged_first(log)
    assert event.responses == [
        "This action failed unexpectedly. Please try again or resend the command."
    ]
    assert "RuntimeError" in caplog.text
    assert "private database detail" not in caplog.text


async def test_cancel_callback_acknowledges_first_and_persists_cancellation(
    tmp_path: Path,
) -> None:
    bot, queue, storage, _ = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    session = await queue.create_interaction(
        1001,
        "rename",
        "ready",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
    )
    log: list[tuple[str, str]] = []
    record_method(queue, "get_interaction", log)
    record_method(queue, "delete_interaction", log)
    event = FakeCallback(1001, f"tg:{session.id}:cancel".encode(), log=log)

    await bot.handle_callback(event)

    assert_acknowledged_first(log)
    assert "Action cancelled." in event.last_text


async def test_confirm_step_text_reminds_instead_of_submitting_download(tmp_path: Path) -> None:
    bot, queue, storage, _ = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    await queue.create_interaction(
        1001,
        "rename",
        "confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
        pending_name="renamed.mp4",
    )
    event = FakeEvent(sender_id=1001, text="not a link or command")

    await bot.handle_event(event)

    assert "Confirm file organization" in event.responses[0]
    assert event.buttons[0] is not None
    assert await queue.claim_next("worker") is None


async def test_archive_buttons_follow_recent_directory_order(tmp_path: Path) -> None:
    bot, queue, storage, _ = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    assert bot.library is not None
    await bot.library.mkdir(1001, "older")
    await bot.library.mkdir(1001, "recent")
    async with aiosqlite.connect(queue.path) as connection:
        await connection.execute(
            "UPDATE file_audit SET created_at = ? WHERE destination_path = ?",
            (10.0, "archive/older"),
        )
        await connection.execute(
            "UPDATE file_audit SET created_at = ? WHERE destination_path = ?",
            (20.0, "archive/recent"),
        )
        await connection.commit()
    session = await queue.create_interaction(
        1001,
        "archive",
        "ready",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
    )
    log: list[tuple[str, str]] = []
    record_method(queue, "get_interaction", log)
    record_method(bot.library, "directories", log)
    event = FakeCallback(1001, f"tg:{session.id}:archive".encode(), log=log)

    await bot.handle_callback(event)

    assert_acknowledged_first(log)
    buttons = event.edit_buttons[0]
    assert isinstance(buttons, list)
    assert [row[0].text for row in buttons] == ["recent", "older", "New directory", "Cancel"]


async def test_new_directory_confirmation_creates_directory_and_moves_file(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    await TelegramJobNotifier(client, queue).job_finished(completed)
    actions = client.sent[0][2]
    assert isinstance(actions, list)

    choose = FakeCallback(1001, button_data(actions[1][0]))
    await bot.handle_callback(choose)
    directory_buttons = choose.edit_buttons[0]
    assert isinstance(directory_buttons, list)
    new_prompt = FakeCallback(1001, button_data(directory_buttons[-2][0]))
    await bot.handle_callback(new_prompt)
    assert "Send a relative path, for example: videos/2026." in new_prompt.last_text

    directory_input = FakeEvent(sender_id=1001, text="videos/2026")
    await bot.handle_event(directory_input)
    assert "Confirm file organization" in client.last_text
    assert "will be created" in client.last_text
    confirmation = client.last_buttons
    assert isinstance(confirmation, list)

    confirm = FakeCallback(1001, button_data(confirmation[0][0]))
    await bot.handle_callback(confirm)

    stored = await queue.get_file(completed.id)
    assert (storage.archive / "videos" / "2026").is_dir()
    assert stored is not None
    assert stored.relative_path == Path("archive/videos/2026/clip.mp4")
    assert "Completed" in confirm.last_text
    assert await queue.latest_interaction_for_actor(1001) is None


async def test_new_directory_confirmation_accepts_existing_directory(
    tmp_path: Path,
) -> None:
    bot, queue, storage, _ = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos/2026")
    await queue.create_interaction(
        1001,
        "archive",
        "await-new-directory",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
    )

    directory_input = FakeEvent(sender_id=1001, text="videos/2026")
    await bot.handle_event(directory_input)
    confirmation = directory_input.buttons[0]
    assert isinstance(confirmation, list)
    confirm = FakeCallback(
        1001,
        button_data(confirmation[0][0]),
        message_id=directory_input.response_ids[-1],
    )
    await bot.handle_callback(confirm)

    stored = await queue.get_file(completed.id)
    assert stored is not None
    assert stored.relative_path == Path("archive/videos/2026/clip.mp4")
    assert "Completed" in confirm.last_text


async def test_senderless_callback_reports_authorization_failure_without_lookup(
    tmp_path: Path,
) -> None:
    bot, queue, storage, _ = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    session = await queue.create_interaction(
        1001,
        "keep",
        "ready",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
    )
    log: list[tuple[str, str]] = []
    record_method(queue, "get_interaction", log)
    event = FakeCallback(None, f"tg:{session.id}:keep".encode(), log=log)

    await bot.handle_callback(event)

    assert_acknowledged_first(log)
    assert ("dependency", "get_interaction") not in log
    assert event.edits == ["Unauthorized."]
    assert session.id.hex not in event.edits[0]


async def test_callback_continues_when_acknowledgement_raises(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot, queue, storage, _ = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    session = await queue.create_interaction(
        1001,
        "keep",
        "ready",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
    )
    event = FakeCallback(1001, f"tg:{session.id}:keep".encode())

    async def fail_answer(text: str = "") -> None:
        event.calls.append(("answer", text))
        raise RuntimeError("acknowledgement transport detail")

    event.answer = fail_answer  # type: ignore[method-assign]
    caplog.set_level(logging.WARNING, logger="tgharbor.telegram.bot")

    await bot.handle_callback(event)

    assert "Kept in Inbox" in event.last_text
    assert "acknowledgement failed" in caplog.text
    assert "acknowledgement transport detail" not in caplog.text
    assert await queue.get_interaction(session.id) is None


async def test_replayed_action_callback_after_name_input_keeps_pending_value(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    await TelegramJobNotifier(client, queue).job_finished(completed)
    actions = client.sent[0][2]
    assert isinstance(actions, list)
    rename_data = button_data(actions[0][1])

    choose = FakeCallback(1001, rename_data)
    await bot.handle_callback(choose)
    name = FakeEvent(sender_id=1001, text="kept-name.mp4")
    await bot.handle_event(name)

    replay = FakeCallback(1001, rename_data)
    await bot.handle_callback(replay)

    session = await queue.latest_interaction_for_actor(1001)
    assert session is not None
    assert session.step == "confirm"
    assert session.pending_name == "kept-name.mp4"
    assert replay.responses == ["This action is stale or invalid."]
    stored = await queue.get_file(completed.id)
    assert stored is not None and stored.current_name == "clip.mp4"


async def test_replayed_action_callback_after_directory_input_keeps_pending_value(
    tmp_path: Path,
) -> None:
    bot, queue, storage, _ = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    session = await queue.create_interaction(
        1001,
        "archive",
        "ready",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
    )
    archive_data = f"tg:{session.id}:archive".encode()

    choose = FakeCallback(1001, archive_data)
    await bot.handle_callback(choose)
    directory = FakeEvent(sender_id=1001, text="videos")
    await bot.handle_event(directory)

    replay = FakeCallback(1001, archive_data)
    await bot.handle_callback(replay)

    session = await queue.latest_interaction_for_actor(1001)
    assert session is not None
    assert session.step == "confirm"
    assert session.pending_directory == "videos"
    assert replay.responses == ["This action is stale or invalid."]
    stored = await queue.get_file(completed.id)
    assert stored is not None
    assert stored.relative_path == Path("inbox/clip.mp4")


async def test_out_of_step_and_cross_action_tokens_are_stale_without_mutation(
    tmp_path: Path,
) -> None:
    bot, queue, storage, _ = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    archive_session = await queue.create_interaction(
        1001,
        "archive",
        "await-directory",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
    )
    rename_session = await queue.create_interaction(
        1001,
        "rename",
        "await-name",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
    )

    events = [
        FakeCallback(1001, f"tg:{archive_session.id}:confirm".encode()),
        FakeCallback(1001, f"tg:{archive_session.id}:rename".encode()),
        FakeCallback(1001, f"tg:{rename_session.id}:new-directory".encode()),
    ]
    for event in events:
        await bot.handle_callback(event)

    for event in events:
        assert event.responses == ["This action is stale or invalid."]
    unchanged_archive = await queue.get_interaction(archive_session.id)
    unchanged_rename = await queue.get_interaction(rename_session.id)
    assert unchanged_archive is not None and unchanged_archive.step == "await-directory"
    assert unchanged_rename is not None and unchanged_rename.step == "await-name"
    stored = await queue.get_file(completed.id)
    assert stored is not None and stored.relative_path == Path("inbox/clip.mp4")


async def test_domain_failure_does_not_echo_absolute_path_or_log_details(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot, queue, storage, _ = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    assert bot.library is not None
    session = await queue.create_interaction(
        1001,
        "rename",
        "confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
        pending_name="safe.mp4",
    )
    absolute_path = "/srv/private/tgharbor/absolute-path/stored-file"

    async def fail_organize(*args: object, **kwargs: object) -> object:
        raise RuntimeError(absolute_path)

    bot.library.organize = fail_organize  # type: ignore[method-assign]
    caplog.set_level(logging.WARNING, logger="tgharbor.telegram.bot")
    event = FakeCallback(1001, f"tg:{session.id}:confirm".encode())

    await bot.handle_callback(event)

    expected = (
        "The file could not be organized because the library state changed. Refresh and try again."
    )
    assert expected in event.last_text
    assert absolute_path not in event.last_text
    assert absolute_path not in caplog.text
    assert "error_type=RuntimeError" in caplog.text


def media_message(
    message_id: int,
    *,
    grouped_id: int = 700,
    forward: object | None = None,
    chat_id: int = 1001,
):
    return SimpleNamespace(
        id=message_id,
        file=object(),
        grouped_id=grouped_id,
        forward=forward,
        chat_id=chat_id,
    )


def forwarded_peer(
    chat_id: int | None,
    *,
    channel: bool = False,
    group: bool = False,
):
    return SimpleNamespace(chat_id=chat_id, is_channel=channel, is_group=group)


class FakeAlbumEvent:
    def __init__(
        self,
        sender_id: int,
        messages: list[object],
        *,
        chat_id: int = 1001,
        grouped_id: int = 700,
    ) -> None:
        self.sender_id = sender_id
        self.messages = messages
        self.chat_id = chat_id
        self.grouped_id = grouped_id
        self.is_private = True
        self.responses: list[str] = []
        self.buttons: list[object | None] = []

    async def respond(self, text: str, *, buttons: object | None = None) -> None:
        self.responses.append(text)
        self.buttons.append(buttons)


def is_move_all_message(buttons: object | None) -> bool:
    if not buttons:
        return False
    labels = {
        getattr(button, "text", "")
        for row in buttons  # type: ignore[union-attr]
        for button in row
    }
    return "Move all to new folder" in labels


class MoveAllFailingClient(FakeClient):
    async def send_message(
        self, actor_id: int, text: str, *, buttons: object | None = None
    ) -> None:
        if is_move_all_message(buttons):
            raise RuntimeError("private batch transport failure")
        await super().send_message(actor_id, text, buttons=buttons)


class FlakyMoveAllClient(FakeClient):
    def __init__(self, *, failures: int = 1) -> None:
        super().__init__()
        self.failures = failures

    async def send_message(
        self, actor_id: int, text: str, *, buttons: object | None = None
    ) -> None:
        if is_move_all_message(buttons) and self.failures > 0:
            self.failures -= 1
            raise RuntimeError("private transient batch transport failure")
        await super().send_message(actor_id, text, buttons=buttons)


class BlockingMoveAllClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def send_message(
        self, actor_id: int, text: str, *, buttons: object | None = None
    ) -> None:
        if is_move_all_message(buttons):
            self.started.set()
            await asyncio.Event().wait()
        await super().send_message(actor_id, text, buttons=buttons)


def move_all_buttons(client: FakeClient) -> list[object]:
    found = []
    for _, _, buttons in client.sent:
        if not buttons:
            continue
        for row in buttons:  # type: ignore[union-attr]
            for button in row:
                if getattr(button, "text", None) == "Move all to new folder":
                    found.append(button)
    return found


async def test_album_submits_every_media_with_one_opaque_batch_id(tmp_path: Path) -> None:
    bot, queue, _, _ = await full_controller(tmp_path)
    album = FakeAlbumEvent(
        1001,
        [media_message(10), media_message(11), media_message(12)],
        grouped_id=777,
    )

    await bot.handle_album(album)

    jobs = await queue.latest_for_requester(1001)
    assert len(jobs) == 3
    assert {job.source_message_id for job in jobs} == {10, 11, 12}
    assert all(job.batch_expected_size == 3 for job in jobs)
    assert len({job.batch_id for job in jobs}) == 1
    batch_id = jobs[0].batch_id
    assert batch_id is not None and len(batch_id) == 32
    assert "777" not in batch_id
    assert "1001" not in batch_id
    assert album.responses == [
        "Album queued · 3 files\nStatus: pending\nMove all appears after all files complete."
    ]


async def test_grouped_new_message_is_not_submitted_twice(tmp_path: Path) -> None:
    bot, queue = await controller(tmp_path)
    event = FakeEvent(sender_id=1001, media=object(), file=object())
    event.message.grouped_id = 1234

    await bot.handle_event(event)

    assert event.responses == []
    assert await queue.latest_for_requester(1001) == []


async def test_album_batch_identity_scopes_requester_source_and_client(
    tmp_path: Path,
) -> None:
    bot, queue, _, _ = await full_controller(tmp_path / "primary")
    first = FakeAlbumEvent(1001, [media_message(1)], chat_id=111, grouped_id=900)
    await bot.handle_album(first)
    first_batch = (await queue.latest_for_requester(1001))[0].batch_id
    await bot.handle_album(first)
    assert (await queue.latest_for_requester(1001))[0].batch_id == first_batch

    await bot.handle_album(FakeAlbumEvent(2002, [media_message(1)], chat_id=111, grouped_id=900))
    other_requester = (await queue.latest_for_requester(2002))[0].batch_id

    await bot.handle_album(FakeAlbumEvent(1001, [media_message(2)], chat_id=222, grouped_id=900))
    other_source = (await queue.latest_for_requester(1001))[0].batch_id

    other_bot, other_queue, _, _ = await full_controller(
        tmp_path / "other",
        batch_identity="different-client",
    )
    await other_bot.handle_album(
        FakeAlbumEvent(1001, [media_message(1)], chat_id=111, grouped_id=900)
    )
    other_client = (await other_queue.latest_for_requester(1001))[0].batch_id

    assert len({first_batch, other_requester, other_source, other_client}) == 4


async def test_forwarded_album_metadata_is_conservative(tmp_path: Path) -> None:
    channel_bot, channel_queue, _, _ = await full_controller(tmp_path / "channel")
    channel_forward = forwarded_peer(-100111, channel=True)
    await channel_bot.handle_album(
        FakeAlbumEvent(
            1001,
            [
                media_message(1, forward=channel_forward),
                media_message(2, forward=channel_forward),
            ],
        )
    )
    channel_jobs = await channel_queue.latest_for_requester(1001)
    assert {job.origin_group_id for job in channel_jobs} == {"-100111"}
    assert {job.channel_id for job in channel_jobs} == {"-100111"}

    group_bot, group_queue, _, _ = await full_controller(tmp_path / "group")
    group_forward = forwarded_peer(-222, group=True)
    await group_bot.handle_album(
        FakeAlbumEvent(
            1001,
            [media_message(3, forward=group_forward), media_message(4, forward=group_forward)],
        )
    )
    group_jobs = await group_queue.latest_for_requester(1001)
    assert {job.origin_group_id for job in group_jobs} == {"-222"}
    assert {job.channel_id for job in group_jobs} == {None}

    user_bot, user_queue, _, _ = await full_controller(tmp_path / "user")
    user_forward = forwarded_peer(None)
    await user_bot.handle_album(
        FakeAlbumEvent(
            1001,
            [media_message(5, forward=user_forward), media_message(6, forward=user_forward)],
        )
    )
    user_jobs = await user_queue.latest_for_requester(1001)
    assert {job.origin_group_id for job in user_jobs} == {None}
    assert {job.channel_id for job in user_jobs} == {None}

    ambiguous_bot, ambiguous_queue, _, _ = await full_controller(tmp_path / "ambiguous")
    await ambiguous_bot.handle_album(
        FakeAlbumEvent(
            1001,
            [
                media_message(7, forward=forwarded_peer(-1001, channel=True)),
                media_message(8, forward=forwarded_peer(-1002, channel=True)),
            ],
        )
    )
    ambiguous_jobs = await ambiguous_queue.latest_for_requester(1001)
    assert {job.origin_group_id for job in ambiguous_jobs} == {None}
    assert {job.channel_id for job in ambiguous_jobs} == {None}


async def test_megagroup_album_metadata_renders_group_without_channel(tmp_path: Path) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    megagroup = forwarded_peer(-100777, channel=True, group=True)
    await bot.handle_album(
        FakeAlbumEvent(
            1001,
            [
                media_message(1, forward=megagroup),
                media_message(2, forward=megagroup),
            ],
        )
    )

    jobs = await queue.latest_for_requester(1001)
    assert {job.origin_group_id for job in jobs} == {"-100777"}
    assert {job.channel_id for job in jobs} == {None}

    running = await queue.claim_next("worker")
    assert running is not None
    assert running.origin_group_id == "-100777"
    assert running.channel_id is None
    running = await queue.update_metadata(
        running.id,
        "worker",
        display_title="Megagroup video",
        original_file_name="album.mp4",
        mime_type="video/mp4",
        total_bytes=2,
    )
    running = await queue.reserve_name(running.id, "worker", "album.mp4")
    target = await storage.prepare(running, "album.mp4")
    target.temporary_path.write_bytes(b"xx")
    output = await storage.commit(target)
    assert running.started_at is not None
    completed = await queue.mark_completed(
        running.id,
        "worker",
        output,
        relative_path=storage.relative_path(output),
        now=running.started_at.timestamp() + 10,
    )

    await TelegramJobNotifier(client, queue).job_finished(completed)

    text = client.sent[0][1]
    assert "Origin Group: -100777" in text
    assert "Channel ID: N/A" in text


async def test_single_job_keeps_expected_size_one_and_no_move_all(tmp_path: Path) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage, message_id=700)
    await TelegramJobNotifier(client, queue).job_finished(completed)

    event = FakeEvent(sender_id=1001, media=object(), file=object(), message_id=701)
    await bot.handle_event(event)
    single = (await queue.latest_for_requester(1001))[0]

    assert single.batch_id is None
    assert single.batch_expected_size == 1
    assert move_all_buttons(client) == []


async def test_completion_message_uses_persisted_catalog_timing_and_privacy(
    tmp_path: Path,
) -> None:
    _, queue, storage, client = await full_controller(tmp_path)
    completed = await completed_file(
        queue,
        storage,
        message_id=800,
        total_bytes=1572864,
        started_at=1000,
        finished_at=1010,
        origin_group_id="-100123",
        channel_id="-100456",
    )

    await TelegramJobNotifier(client, queue).job_finished(completed)

    start = datetime.fromtimestamp(1000, UTC).astimezone().strftime("%H:%M:%S")
    end = datetime.fromtimestamp(1010, UTC).astimezone().strftime("%H:%M:%S")
    text = client.sent[0][1]
    assert text == (
        "Download Completed\n"
        f"Job: {completed.short_id}\n"
        "File Name: clip.mp4\n"
        "Download Folder: /download/inbox\n"
        "File Size: 1.50 MB\n"
        f"Start Time: {start}\n"
        f"End Time: {end}\n"
        "Download Time: 10.00\n"
        "Download Speed: 153.60 KB/s\n"
        "User Id: 1001\n"
        "Origin Group: -100123\n"
        "Channel ID: -100456"
    )
    assert str(tmp_path) not in text
    assert client.sent[0][2] is not None


async def test_completion_message_handles_missing_metadata() -> None:
    now = datetime.now(UTC)
    job = DownloadJob(
        id=uuid4(),
        requester_id=1001,
        source_session=SourceSession.BOT,
        source_chat="1001",
        source_message_id=1,
        destination_name=None,
        status=JobStatus.COMPLETED,
        attempt_count=1,
        max_attempts=3,
        progress_bytes=0,
        total_bytes=0,
        output_path=None,
        error=None,
        available_at=now,
        created_at=now,
        updated_at=now,
        claimed_by=None,
        cancel_requested=False,
        reserved_name="reserved.bin",
    )

    text = _completion_message(job, None)

    assert "File Name: reserved.bin" in text
    assert "Download Folder: N/A" in text
    assert "File Size: 0.00 MB" in text
    assert "Start Time: N/A" in text
    assert "End Time: N/A" in text
    assert "Download Time: N/A" in text
    assert "Download Speed: N/A" in text
    assert "Origin Group: N/A" in text
    assert "Channel ID: N/A" in text


async def test_zero_elapsed_guards_timing_and_speed(tmp_path: Path) -> None:
    _, queue, storage, client = await full_controller(tmp_path)
    completed = await completed_file(
        queue,
        storage,
        message_id=801,
        started_at=1000,
        finished_at=1000,
    )

    await TelegramJobNotifier(client, queue).job_finished(completed)

    text = client.sent[0][1]
    assert "Download Time: N/A" in text
    assert "Download Speed: N/A" in text


async def test_completion_metadata_query_failure_keeps_summary_delivery(
    tmp_path: Path,
) -> None:
    _, queue, storage, client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage, message_id=802)

    async def fail_lookup(*args: object, **kwargs: object) -> object:
        raise RuntimeError("private host path detail")

    queue.get_file = fail_lookup  # type: ignore[method-assign]
    await TelegramJobNotifier(client, queue).job_finished(completed)

    text = client.sent[0][1]
    assert "Download Completed" in text
    assert "Download Folder: N/A" in text
    assert "private host path detail" not in text


async def test_batch_notice_waits_for_all_members_and_is_sent_once(tmp_path: Path) -> None:
    _, queue, storage, client = await full_controller(tmp_path)
    notifier = TelegramJobNotifier(client, queue)
    first = await completed_file(
        queue,
        storage,
        message_id=900,
        batch_id="batch-single-notice",
        batch_expected_size=2,
    )
    await notifier.job_finished(first)
    assert move_all_buttons(client) == []

    second = await completed_file(
        queue,
        storage,
        message_id=901,
        batch_id="batch-single-notice",
        batch_expected_size=2,
    )
    await notifier.job_finished(second)
    await notifier.job_finished(second)

    assert len(move_all_buttons(client)) == 1


async def test_concurrent_batch_completion_sends_one_notice(tmp_path: Path) -> None:
    _, queue, storage, client = await full_controller(tmp_path)
    first = await completed_file(
        queue,
        storage,
        message_id=910,
        batch_id="batch-concurrent-notice",
        batch_expected_size=2,
    )
    second = await completed_file(
        queue,
        storage,
        message_id=911,
        batch_id="batch-concurrent-notice",
        batch_expected_size=2,
    )

    notifier = TelegramJobNotifier(client, queue)
    await asyncio.gather(notifier.job_finished(first), notifier.job_finished(second))

    assert len(move_all_buttons(client)) == 1


async def test_batch_transient_send_failure_is_retried_without_another_completion(
    tmp_path: Path,
) -> None:
    _, queue, storage, _ = await full_controller(tmp_path)
    await completed_file(
        queue,
        storage,
        message_id=920,
        batch_id="batch-send-failure",
        batch_expected_size=2,
    )
    second = await completed_file(
        queue,
        storage,
        message_id=921,
        batch_id="batch-send-failure",
        batch_expected_size=2,
    )
    client = FlakyMoveAllClient(failures=1)
    notifier = TelegramJobNotifier(client, queue)
    dispatcher = BatchNotificationDispatcher(notifier, poll_interval=3600)

    await notifier.job_finished(second)

    assert move_all_buttons(client) == []
    interaction = await queue.active_batch_move_interaction(1001, "batch-send-failure")
    assert interaction is not None
    assert await batch_notice_state(queue, "batch-send-failure") == "pending"

    assert await dispatcher.dispatch_once() == 1

    buttons = move_all_buttons(client)
    assert len(buttons) == 1
    assert button_data(buttons[0]) == f"tg:{interaction.id}:batch-move".encode()
    assert await batch_notice_state(queue, "batch-send-failure") == "sent"
    assert await dispatcher.dispatch_once() == 0
    assert len(move_all_buttons(client)) == 1


async def test_batch_mark_failure_retries_at_least_once_same_interaction(
    tmp_path: Path,
) -> None:
    _, queue, storage, _ = await full_controller(tmp_path)
    await completed_file(
        queue,
        storage,
        message_id=925,
        batch_id="batch-mark-failure",
        batch_expected_size=2,
    )
    second = await completed_file(
        queue,
        storage,
        message_id=926,
        batch_id="batch-mark-failure",
        batch_expected_size=2,
    )
    client = FakeClient()
    notifier = TelegramJobNotifier(client, queue)
    dispatcher = BatchNotificationDispatcher(notifier, poll_interval=3600)
    mark_attempts: list[str] = []
    original_mark = queue.mark_batch_notification_sent

    async def flaky_mark(
        batch_id: str, requester_id: int, *, claimed_by: str, now: float | None = None
    ) -> bool:
        mark_attempts.append(claimed_by)
        if len(mark_attempts) == 1:
            raise RuntimeError("private durable mark failure")
        return await original_mark(batch_id, requester_id, claimed_by=claimed_by, now=now)

    queue.mark_batch_notification_sent = flaky_mark  # type: ignore[method-assign]
    try:
        await notifier.job_finished(second)

        assert len(move_all_buttons(client)) == 1
        interaction = await queue.active_batch_move_interaction(1001, "batch-mark-failure")
        assert interaction is not None
        assert await batch_notice_state(queue, "batch-mark-failure") == "pending"

        assert await dispatcher.dispatch_once() == 1
    finally:
        queue.mark_batch_notification_sent = original_mark  # type: ignore[method-assign]

    buttons = move_all_buttons(client)
    assert len(buttons) == 2
    assert {button_data(button) for button in buttons} == {
        f"tg:{interaction.id}:batch-move".encode()
    }
    assert await batch_notice_state(queue, "batch-mark-failure") == "sent"
    assert await dispatcher.dispatch_once() == 0
    assert len(move_all_buttons(client)) == 2


async def test_startup_redrives_crash_after_interaction_before_send(tmp_path: Path) -> None:
    _, queue, storage, _ = await full_controller(tmp_path)
    await completed_file(
        queue,
        storage,
        message_id=930,
        batch_id="batch-crash-before-send",
        batch_expected_size=2,
    )
    await completed_file(
        queue,
        storage,
        message_id=931,
        batch_id="batch-crash-before-send",
        batch_expected_size=2,
    )
    assert await queue.claim_batch_notification(
        "batch-crash-before-send", 1001, claimed_by="crashed", now=1000
    )
    interaction = await queue.ensure_batch_move_interaction(
        1001,
        "batch-crash-before-send",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        now=1000,
    )
    assert await queue.recover_interrupted(now=1001, notice_stale_after_seconds=0) == (0, 0)

    client = FakeClient()
    notifier = TelegramJobNotifier(client, queue)
    dispatcher = BatchNotificationDispatcher(notifier, poll_interval=3600)

    assert await dispatcher.dispatch_once() == 1

    buttons = move_all_buttons(client)
    assert len(buttons) == 1
    assert button_data(buttons[0]) == f"tg:{interaction.id}:batch-move".encode()
    assert await batch_notice_state(queue, "batch-crash-before-send") == "sent"
    assert await dispatcher.dispatch_once() == 0


async def test_dispatch_once_reclaims_claim_stale_by_configured_threshold(
    tmp_path: Path,
) -> None:
    _, queue, storage, _ = await full_controller(tmp_path)
    await completed_file(
        queue,
        storage,
        message_id=955,
        batch_id="batch-custom-stale",
        batch_expected_size=2,
    )
    await completed_file(
        queue,
        storage,
        message_id=956,
        batch_id="batch-custom-stale",
        batch_expected_size=2,
    )
    # Older than the dispatcher's 30 second threshold but younger than the
    # repository default, so only a propagated threshold allows the reclaim.
    assert await queue.claim_batch_notification(
        "batch-custom-stale", 1001, claimed_by="crashed", now=time.time() - 60
    )
    interaction = await queue.ensure_batch_move_interaction(
        1001,
        "batch-custom-stale",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    client = FakeClient()
    notifier = TelegramJobNotifier(client, queue)
    dispatcher = BatchNotificationDispatcher(
        notifier,
        poll_interval=3600,
        stale_after_seconds=30,
    )

    assert await dispatcher.dispatch_once() == 1

    buttons = move_all_buttons(client)
    assert len(buttons) == 1
    assert button_data(buttons[0]) == f"tg:{interaction.id}:batch-move".encode()
    assert await batch_notice_state(queue, "batch-custom-stale") == "sent"
    assert await dispatcher.dispatch_once() == 0
    assert len(move_all_buttons(client)) == 1


async def test_dispatch_once_keeps_claim_fresh_by_configured_threshold(
    tmp_path: Path,
) -> None:
    _, queue, storage, _ = await full_controller(tmp_path)
    await completed_file(
        queue,
        storage,
        message_id=960,
        batch_id="batch-custom-fresh",
        batch_expected_size=2,
    )
    await completed_file(
        queue,
        storage,
        message_id=961,
        batch_id="batch-custom-fresh",
        batch_expected_size=2,
    )
    assert await queue.claim_batch_notification(
        "batch-custom-fresh", 1001, claimed_by="active", now=time.time() - 5
    )

    client = FakeClient()
    notifier = TelegramJobNotifier(client, queue)
    dispatcher = BatchNotificationDispatcher(
        notifier,
        poll_interval=3600,
        stale_after_seconds=30,
    )

    assert await dispatcher.dispatch_once() == 0

    assert move_all_buttons(client) == []
    assert await batch_notice_state(queue, "batch-custom-fresh") == "claimed"


async def test_startup_redrives_existing_pending_notice(tmp_path: Path) -> None:
    _, queue, storage, _ = await full_controller(tmp_path)
    await completed_file(
        queue,
        storage,
        message_id=935,
        batch_id="batch-startup-pending",
        batch_expected_size=2,
    )
    await completed_file(
        queue,
        storage,
        message_id=936,
        batch_id="batch-startup-pending",
        batch_expected_size=2,
    )
    assert await queue.claim_batch_notification(
        "batch-startup-pending", 1001, claimed_by="prepared"
    )
    assert await queue.release_batch_notification(
        "batch-startup-pending", 1001, claimed_by="prepared"
    )

    client = FakeClient()
    notifier = TelegramJobNotifier(client, queue)
    dispatcher = BatchNotificationDispatcher(notifier, poll_interval=3600)

    assert await dispatcher.dispatch_once() == 1
    assert len(move_all_buttons(client)) == 1
    assert await batch_notice_state(queue, "batch-startup-pending") == "sent"


async def test_dispatcher_and_completion_converge_on_one_notice(tmp_path: Path) -> None:
    _, queue, storage, client = await full_controller(tmp_path)
    await completed_file(
        queue,
        storage,
        message_id=940,
        batch_id="batch-concurrent-outbox",
        batch_expected_size=2,
    )
    second = await completed_file(
        queue,
        storage,
        message_id=941,
        batch_id="batch-concurrent-outbox",
        batch_expected_size=2,
    )
    notifier = TelegramJobNotifier(client, queue)
    dispatcher = BatchNotificationDispatcher(notifier, poll_interval=3600)

    await asyncio.gather(notifier.job_finished(second), dispatcher.dispatch_once())

    assert len(move_all_buttons(client)) == 1
    assert await batch_notice_state(queue, "batch-concurrent-outbox") == "sent"
    assert await dispatcher.dispatch_once() == 0


async def test_sent_notice_is_never_redelivered(tmp_path: Path) -> None:
    _, queue, storage, client = await full_controller(tmp_path)
    await completed_file(
        queue,
        storage,
        message_id=945,
        batch_id="batch-never-redeliver",
        batch_expected_size=2,
    )
    second = await completed_file(
        queue,
        storage,
        message_id=946,
        batch_id="batch-never-redeliver",
        batch_expected_size=2,
    )
    notifier = TelegramJobNotifier(client, queue)
    dispatcher = BatchNotificationDispatcher(notifier, poll_interval=3600)

    await notifier.job_finished(second)
    assert len(move_all_buttons(client)) == 1

    assert await dispatcher.dispatch_once() == 0
    await notifier.job_finished(second)
    assert await dispatcher.dispatch_once() == 0
    assert len(move_all_buttons(client)) == 1


async def test_dispatcher_polls_without_busy_loop_and_stops(tmp_path: Path) -> None:
    _, queue, _, _ = await full_controller(tmp_path)
    notifier = TelegramJobNotifier(FakeClient(), queue)
    dispatcher = BatchNotificationDispatcher(notifier, poll_interval=3600)
    calls = 0
    listed = asyncio.Event()
    original_list = queue.list_claimable_batch_notifications

    async def counting_list(
        *,
        now: float | None = None,
        stale_after_seconds: float = 300,
        limit: int = 20,
    ) -> list[object]:
        nonlocal calls
        calls += 1
        listed.set()
        return await original_list(
            now=now,
            stale_after_seconds=stale_after_seconds,
            limit=limit,
        )

    queue.list_claimable_batch_notifications = counting_list  # type: ignore[method-assign]
    await dispatcher.start()
    try:
        dispatcher.notify()
        await asyncio.wait_for(listed.wait(), timeout=1)
        for _ in range(3):
            await asyncio.sleep(0)
        assert calls == 1
        assert dispatcher.running
    finally:
        await dispatcher.stop()

    assert not dispatcher.running
    assert calls == 1


async def test_dispatcher_stop_cancels_send_without_changing_job_state(
    tmp_path: Path,
) -> None:
    _, queue, storage, _ = await full_controller(tmp_path)
    first = await completed_file(
        queue,
        storage,
        message_id=950,
        batch_id="batch-cancelled-send",
        batch_expected_size=2,
    )
    second = await completed_file(
        queue,
        storage,
        message_id=951,
        batch_id="batch-cancelled-send",
        batch_expected_size=2,
    )
    client = BlockingMoveAllClient()
    notifier = TelegramJobNotifier(client, queue)
    dispatcher = BatchNotificationDispatcher(notifier, poll_interval=3600)

    await dispatcher.start()
    dispatcher.notify()
    await asyncio.wait_for(client.started.wait(), timeout=1)
    await asyncio.wait_for(dispatcher.stop(), timeout=1)

    assert not dispatcher.running
    assert (await queue.get(first.id)).status is JobStatus.COMPLETED
    assert (await queue.get(second.id)).status is JobStatus.COMPLETED
    assert await batch_notice_state(queue, "batch-cancelled-send") == "pending"
    assert await queue.claim_batch_notification("batch-cancelled-send", 1001, claimed_by="probe")


async def test_batch_directory_selection_rejects_reordering_without_mutation(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    jobs = [
        await completed_file(
            queue,
            storage,
            message_id=927 + index,
            batch_id="batch-move-reorder",
            batch_expected_size=2,
        )
        for index in range(2)
    ]
    assert bot.library is not None
    await bot.library.mkdir(1001, "older")
    await bot.library.mkdir(1001, "recent")
    async with aiosqlite.connect(queue.path) as connection:
        await connection.execute(
            "UPDATE file_audit SET created_at = ? WHERE destination_path = ?",
            (10.0, "archive/older"),
        )
        await connection.execute(
            "UPDATE file_audit SET created_at = ? WHERE destination_path = ?",
            (20.0, "archive/recent"),
        )
        await connection.commit()
    notifier = TelegramJobNotifier(client, queue)
    for job in jobs:
        await notifier.job_finished(job)

    opened = FakeCallback(1001, button_data(move_all_buttons(client)[0]))
    await bot.handle_callback(opened)
    rendered = opened.edit_buttons[0]
    assert isinstance(rendered, list)
    assert [row[0].text for row in rendered] == ["recent", "older", "New directory", "Cancel"]
    session = await queue.latest_interaction_for_actor(1001)
    assert session is not None

    async with aiosqlite.connect(queue.path) as connection:
        await connection.execute(
            "UPDATE file_audit SET created_at = ? WHERE destination_path = ?",
            (30.0, "archive/older"),
        )
        await connection.commit()

    stale_click = FakeCallback(1001, button_data(rendered[0][0]))
    await bot.handle_callback(stale_click)

    assert "stale" in stale_click.last_text
    refreshed = stale_click.edit_buttons[0]
    assert isinstance(refreshed, list)
    assert [row[0].text for row in refreshed] == ["older", "recent", "New directory", "Cancel"]
    unchanged = await queue.get_interaction(session.id)
    assert unchanged is not None
    assert unchanged.step == "await-directory"
    assert unchanged.pending_directory is None
    for job in jobs:
        stored = await queue.get_file(job.id)
        assert stored is not None
        assert stored.relative_path.parts[0] == "inbox"

    select = FakeCallback(1001, button_data(refreshed[1][0]))
    await bot.handle_callback(select)
    assert "Confirm batch move" in select.last_text
    assert "Directory: recent" in select.last_text
    confirmation = select.edit_buttons[0]
    assert isinstance(confirmation, list)
    confirm = FakeCallback(1001, button_data(confirmation[0][0]))
    await bot.handle_callback(confirm)

    assert "Moved: 2" in confirm.last_text
    for job in jobs:
        stored = await queue.get_file(job.id)
        assert stored is not None
        assert stored.relative_path.parent == Path("archive/recent")


async def test_batch_partial_move_keeps_retry_action_and_retry_succeeds(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    jobs = [
        await completed_file(
            queue,
            storage,
            message_id=935 + index,
            batch_id="batch-move-retry",
            batch_expected_size=2,
        )
        for index in range(2)
    ]
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    notifier = TelegramJobNotifier(client, queue)
    for job in jobs:
        await notifier.job_finished(job)

    blocked = await queue.get_file(jobs[1].id)
    assert blocked is not None
    blocked_path = storage.root / blocked.relative_path
    blocked_bytes = blocked_path.read_bytes()
    blocked_path.unlink()

    opened = FakeCallback(1001, button_data(move_all_buttons(client)[0]))
    await bot.handle_callback(opened)
    directories = opened.edit_buttons[0]
    assert isinstance(directories, list)
    select = FakeCallback(1001, button_data(directories[0][0]))
    await bot.handle_callback(select)
    confirmation = select.edit_buttons[0]
    assert isinstance(confirmation, list)
    confirm_data = button_data(confirmation[0][0])
    confirm = FakeCallback(1001, confirm_data)
    await bot.handle_callback(confirm)

    assert "Moved: 1" in confirm.last_text
    assert "Failed: 1" in confirm.last_text
    assert "Choose a directory again to retry" in confirm.last_text
    retry_buttons = confirm.edit_buttons[-1]
    assert isinstance(retry_buttons, list)
    assert retry_buttons[0][0].text == "Retry move"
    session_id = UUID(confirm_data.decode().split(":")[1])
    retained = await queue.get_interaction(session_id)
    assert retained is not None
    assert retained.step == "ready"
    assert retained.pending_directory is None
    assert retained.batch_id == "batch-move-retry"

    replay_confirm = FakeCallback(1001, confirm_data)
    await bot.handle_callback(replay_confirm)
    assert "stale" in replay_confirm.responses[0]
    still_inbox = await queue.get_file(jobs[1].id)
    assert still_inbox is not None
    assert still_inbox.relative_path.parts[0] == "inbox"

    blocked_path.write_bytes(blocked_bytes)
    retry = FakeCallback(1001, button_data(retry_buttons[0][0]))
    await bot.handle_callback(retry)
    assert "Choose where to move all files" in retry.last_text
    retry_directories = retry.edit_buttons[0]
    assert isinstance(retry_directories, list)
    retry_select = FakeCallback(1001, button_data(retry_directories[0][0]))
    await bot.handle_callback(retry_select)
    retry_confirmation = retry_select.edit_buttons[0]
    assert isinstance(retry_confirmation, list)
    retry_confirm = FakeCallback(1001, button_data(retry_confirmation[0][0]))
    await bot.handle_callback(retry_confirm)

    assert "Moved: 1" in retry_confirm.last_text
    assert "Already moved: 1" in retry_confirm.last_text
    assert "Failed: 0" in retry_confirm.last_text
    for job in jobs:
        stored = await queue.get_file(job.id)
        assert stored is not None
        assert stored.relative_path.parent == Path("archive/videos")
    assert await queue.get_interaction(session_id) is None

    replay_retry = FakeCallback(1001, button_data(retry_buttons[0][0]))
    await bot.handle_callback(replay_retry)
    assert "stale" in replay_retry.responses[0]


async def test_batch_move_callback_uses_recent_directories_and_existing_target(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    jobs = [
        await completed_file(
            queue,
            storage,
            message_id=930 + index,
            batch_id="batch-move-existing",
            batch_expected_size=2,
        )
        for index in range(2)
    ]
    notifier = TelegramJobNotifier(client, queue)
    for job in jobs:
        await notifier.job_finished(job)
    move_all = move_all_buttons(client)
    assert len(move_all) == 1

    assert bot.library is not None
    await bot.library.mkdir(1001, "older")
    await bot.library.mkdir(1001, "recent")
    async with aiosqlite.connect(queue.path) as connection:
        await connection.execute(
            "UPDATE file_audit SET created_at = ? WHERE destination_path = ?",
            (10.0, "archive/older"),
        )
        await connection.execute(
            "UPDATE file_audit SET created_at = ? WHERE destination_path = ?",
            (20.0, "archive/recent"),
        )
        await connection.commit()

    opened = FakeCallback(1001, button_data(move_all[0]))
    await bot.handle_callback(opened)
    assert_acknowledged_first(opened.calls)
    directories = opened.edit_buttons[0]
    assert isinstance(directories, list)
    assert [row[0].text for row in directories] == ["recent", "older", "New directory", "Cancel"]

    select = FakeCallback(1001, button_data(directories[0][0]))
    await bot.handle_callback(select)
    assert "Confirm batch move" in select.last_text
    confirmation = select.edit_buttons[0]
    assert isinstance(confirmation, list)
    confirm = FakeCallback(1001, button_data(confirmation[0][0]))
    await bot.handle_callback(confirm)

    for job in jobs:
        stored = await queue.get_file(job.id)
        assert stored is not None
        assert stored.relative_path.parent == Path("archive/recent")
    assert "Moved: 2" in confirm.last_text
    assert "Skipped: 0" in confirm.last_text
    assert "Failed: 0" in confirm.last_text
    assert str(tmp_path) not in confirm.last_text


async def test_batch_move_new_directory_prompts_then_confirms_creation(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    jobs = [
        await completed_file(
            queue,
            storage,
            message_id=940 + index,
            batch_id="batch-move-new",
            batch_expected_size=2,
        )
        for index in range(2)
    ]
    notifier = TelegramJobNotifier(client, queue)
    for job in jobs:
        await notifier.job_finished(job)

    opened = FakeCallback(1001, button_data(move_all_buttons(client)[0]))
    await bot.handle_callback(opened)
    directory_buttons = opened.edit_buttons[0]
    assert isinstance(directory_buttons, list)
    new_directory = FakeCallback(1001, button_data(directory_buttons[-2][0]))
    await bot.handle_callback(new_directory)
    assert "Send a relative path, for example: videos/2026." in new_directory.last_text

    text = FakeEvent(sender_id=1001, text="videos/2026")
    await bot.handle_event(text)
    assert "Confirm batch move" in client.last_text
    assert "will be created" in client.last_text
    confirmation = client.last_buttons
    assert isinstance(confirmation, list)
    confirm = FakeCallback(1001, button_data(confirmation[0][0]))
    await bot.handle_callback(confirm)

    assert (storage.archive / "videos" / "2026").is_dir()
    for job in jobs:
        stored = await queue.get_file(job.id)
        assert stored is not None
        assert stored.relative_path.parent == Path("archive/videos/2026")
    assert "Moved: 2" in confirm.last_text


async def test_batch_move_reports_partial_and_already_moved_counts(tmp_path: Path) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    jobs = [
        await completed_file(
            queue,
            storage,
            message_id=950 + index,
            batch_id="batch-move-partial",
            batch_expected_size=2,
        )
        for index in range(2)
    ]
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    await bot.library.organize(1001, jobs[0].id.hex, archive_directory="videos")
    notifier = TelegramJobNotifier(client, queue)
    for job in jobs:
        await notifier.job_finished(job)

    opened = FakeCallback(1001, button_data(move_all_buttons(client)[0]))
    await bot.handle_callback(opened)
    directories = opened.edit_buttons[0]
    assert isinstance(directories, list)
    select = FakeCallback(1001, button_data(directories[0][0]))
    await bot.handle_callback(select)
    confirmation = select.edit_buttons[0]
    assert isinstance(confirmation, list)
    confirm = FakeCallback(1001, button_data(confirmation[0][0]))
    await bot.handle_callback(confirm)

    result = confirm.last_text
    assert "Moved: 1" in result
    assert "Skipped: 1" in result
    assert "Failed: 0" in result
    assert "Already moved: 1" in result


async def test_batch_move_replay_stale_expired_and_cross_requester_are_safe(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    jobs = [
        await completed_file(
            queue,
            storage,
            message_id=960 + index,
            batch_id="batch-move-replay",
            batch_expected_size=2,
        )
        for index in range(2)
    ]
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    notifier = TelegramJobNotifier(client, queue)
    for job in jobs:
        await notifier.job_finished(job)
    move_all = move_all_buttons(client)
    assert len(move_all) == 1

    opened = FakeCallback(1001, button_data(move_all[0]))
    await bot.handle_callback(opened)
    directories = opened.edit_buttons[0]
    assert isinstance(directories, list)
    select = FakeCallback(1001, button_data(directories[0][0]))
    await bot.handle_callback(select)
    confirmation = select.edit_buttons[0]
    assert isinstance(confirmation, list)
    confirm_data = button_data(confirmation[0][0])

    cross_requester = FakeCallback(2002, confirm_data)
    await bot.handle_callback(cross_requester)
    assert_acknowledged_first(cross_requester.calls)
    assert "unauthorized" in cross_requester.edits[-1]

    expired_session = await queue.create_interaction(
        1001,
        "batch-move",
        "ready",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        batch_id="batch-move-replay",
    )
    expired = FakeCallback(1001, f"tg:{expired_session.id}:batch-move".encode())
    await bot.handle_callback(expired)
    assert_acknowledged_first(expired.calls)
    assert "expired" in expired.edits[-1]

    cross_action_session = await queue.create_interaction(
        1001,
        "batch-move",
        "ready",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        batch_id="batch-move-replay",
    )
    cross_action = FakeCallback(1001, f"tg:{cross_action_session.id}:confirm".encode())
    await bot.handle_callback(cross_action)
    assert cross_action.responses == ["This action is stale or invalid."]

    confirm = FakeCallback(1001, confirm_data)
    await bot.handle_callback(confirm)
    assert "Moved: 2" in confirm.last_text
    replay = FakeCallback(1001, confirm_data)
    await bot.handle_callback(replay)
    assert_acknowledged_first(replay.calls)
    assert "stale" in replay.responses[0]
    for job in jobs:
        stored = await queue.get_file(job.id)
        assert stored is not None
        assert stored.relative_path.parent == Path("archive/videos")


async def test_startup_notice_zero_and_nonzero_include_expected_actions(
    tmp_path: Path,
) -> None:
    bot, queue, _, client = await full_controller(tmp_path)
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    await bot.send_startup_notice(1001, 0, now=now)
    assert f"TGHarbor {tgharbor.__version__}" in client.sent[-1][1]
    assert now.astimezone().isoformat() in client.sent[-1][1]
    assert "Interrupted tasks: 0" in client.sent[-1][1]
    assert "No interrupted tasks" in client.sent[-1][1]
    assert client.sent[-1][2] is None

    await queue.enqueue(
        NewDownloadJob(
            requester_id=1001,
            source_session=SourceSession.BOT,
            source_chat="1001",
            source_message_id=970,
        )
    )
    running = await queue.claim_next("worker")
    assert running is not None
    await queue.recover_interrupted(now=1000)
    paused = await queue.list_paused_for_requester(1001)
    assert len(paused) == 1

    await bot.send_startup_notice(1001, len(paused), now=now)
    buttons = client.sent[-1][2]
    assert isinstance(buttons, list)
    assert [button.text for button in buttons[0]] == [
        "View interrupted tasks",
        "Continue interrupted tasks",
    ]
    assert await queue.recover_interrupted(now=1001) == (0, 0)
    assert len(await queue.list_paused_for_requester(1001)) == 1


async def test_startup_view_is_requester_scoped_and_denies_cross_user(
    tmp_path: Path,
) -> None:
    bot, queue, _, client = await full_controller(tmp_path)
    for requester, message_id in ((1001, 980), (2002, 981)):
        await queue.enqueue(
            NewDownloadJob(
                requester_id=requester,
                source_session=SourceSession.BOT,
                source_chat=str(requester),
                source_message_id=message_id,
            )
        )
        running = await queue.claim_next(f"worker-{requester}")
        assert running is not None
        await queue.recover_interrupted(now=1000)
    paused = await queue.list_paused_for_requester(1001)
    other_paused = await queue.list_paused_for_requester(2002)
    assert len(paused) == 1 and len(other_paused) == 1

    await bot.send_startup_notice(1001, len(paused), now=datetime.now(UTC))
    view_data = button_data(client.sent[-1][2][0][0])
    view = FakeCallback(1001, view_data)
    await bot.handle_callback(view)
    assert_acknowledged_first(view.calls)
    assert paused[0].short_id in view.responses[0]
    assert other_paused[0].short_id not in view.responses[0]
    assert str(tmp_path) not in view.responses[0]

    cross_user = FakeCallback(2002, view_data)
    await bot.handle_callback(cross_user)
    assert_acknowledged_first(cross_user.calls)
    assert "unauthorized" in cross_user.edits[-1]


async def test_startup_continue_resumes_once_and_wakes_workers(tmp_path: Path) -> None:
    wake: list[str] = []
    bot, queue, _, client = await full_controller(
        tmp_path,
        wake_workers=lambda: wake.append("wake"),
    )
    await queue.enqueue(
        NewDownloadJob(
            requester_id=1001,
            source_session=SourceSession.BOT,
            source_chat="1001",
            source_message_id=990,
        )
    )
    running = await queue.claim_next("worker")
    assert running is not None
    await queue.recover_interrupted(now=1000)
    paused = await queue.list_paused_for_requester(1001)
    await bot.send_startup_notice(1001, len(paused), now=datetime.now(UTC))
    continue_data = button_data(client.sent[-1][2][0][1])

    cross_user = FakeCallback(2002, continue_data)
    await bot.handle_callback(cross_user)
    assert "unauthorized" in cross_user.edits[-1]
    assert len(await queue.list_paused_for_requester(1001)) == 1

    continuation = FakeCallback(1001, continue_data)
    await bot.handle_callback(continuation)
    assert_acknowledged_first(continuation.calls)
    assert continuation.responses == ["Resumed 1 interrupted task(s)."]
    assert await queue.list_paused_for_requester(1001) == []
    assert wake == ["wake"]

    replay = FakeCallback(1001, continue_data)
    await bot.handle_callback(replay)
    assert replay.responses == ["Resumed 0 interrupted task(s)."]
    assert wake == ["wake"]


class CommandClient:
    def __init__(self, *, fail_scopes: set[str] | None = None) -> None:
        self.requests: list[object] = []
        self.fail_scopes = fail_scopes or set()

    async def __call__(self, request: object) -> None:
        self.requests.append(request)
        scope_name = type(request.scope).__name__  # type: ignore[attr-defined]
        if scope_name in self.fail_scopes:
            raise RuntimeError("private registration transport detail")


async def test_register_commands_exact_set_scopes_and_failure_isolation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from telethon.tl.types import BotCommandScopeDefault, BotCommandScopeUsers

    client = CommandClient()
    await register_commands(client)

    assert [type(request.scope) for request in client.requests] == [
        BotCommandScopeDefault,
        BotCommandScopeUsers,
    ]
    commands = client.requests[0].commands  # type: ignore[attr-defined]
    assert [(command.command, command.description) for command in commands] == list(_BOT_COMMANDS)
    assert [command.command for command in commands] == [
        "start",
        "files",
        "status",
        "history",
        "dirs",
        "cancel",
        "stats",
        "rename",
        "archive",
        "mkdir",
        "help",
    ]
    assert all(request.lang_code == "" for request in client.requests)  # type: ignore[attr-defined]

    caplog.set_level(logging.INFO, logger="tgharbor.telegram.bot")
    failing = CommandClient(fail_scopes={"BotCommandScopeDefault"})
    await register_commands(failing)

    assert [type(request.scope) for request in failing.requests] == [
        BotCommandScopeDefault,
        BotCommandScopeUsers,
    ]
    assert "outcome=failed scope=default error_type=RuntimeError" in caplog.text
    assert "outcome=registered scope=all-private" in caplog.text
    assert "private registration transport detail" not in caplog.text


async def test_accepted_messages_include_source_status_and_next_step(tmp_path: Path) -> None:
    bot, queue = await controller(tmp_path)

    link = FakeEvent(sender_id=1001, text="https://t.me/example_channel/88")
    await bot.handle_event(link)
    job = (await queue.latest_for_requester(1001))[0]
    assert link.responses == [
        (
            f"Download queued · {job.short_id}\n"
            "Source: Telegram message link\n"
            "Status: pending\n"
            f"Next: /status {job.short_id}"
        )
    ]

    upload = FakeEvent(sender_id=1001, media=object(), file=object(), message_id=91)
    await bot.handle_event(upload)
    upload_job = next(
        job for job in await queue.latest_for_requester(1001) if job.source_message_id == 91
    )
    assert upload.responses == [
        (
            f"Download queued · {upload_job.short_id}\n"
            "Source: Bot upload\n"
            "Status: pending\n"
            f"Next: /status {upload_job.short_id}"
        )
    ]


async def test_completion_and_failure_summaries_put_job_after_title(tmp_path: Path) -> None:
    _, queue, storage, client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    await TelegramJobNotifier(client, queue).job_finished(completed)

    lines = client.sent[0][1].splitlines()
    assert lines[0] == "Download Completed"
    assert lines[1] == f"Job: {completed.short_id}"
    assert lines[2].startswith("File Name:")

    now = datetime.now(UTC)
    failed = DownloadJob(
        id=uuid4(),
        requester_id=1001,
        source_session=SourceSession.BOT,
        source_chat="1001",
        source_message_id=1,
        destination_name=None,
        status=JobStatus.FAILED,
        attempt_count=1,
        max_attempts=3,
        progress_bytes=0,
        total_bytes=None,
        output_path=None,
        error="download failed",
        available_at=now,
        created_at=now,
        updated_at=now,
        claimed_by=None,
        cancel_requested=False,
        original_file_name="clip.mp4",
    )
    failed_lines = _format_job(failed).splitlines()
    assert failed_lines[0].endswith("clip.mp4")
    assert failed_lines[1] == f"Job: {failed.short_id}"


async def test_single_file_one_card_new_directory_flow_keeps_anchor(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    await TelegramJobNotifier(client, queue).job_finished(completed)
    card = client.sent[0][2]
    assert isinstance(card, list)
    archive_data = button_data(card[1][0])
    assert await queue.claim_next("worker") is None

    choose = FakeCallback(1001, archive_data, message_id=610)
    await bot.handle_callback(choose)
    assert_acknowledged_first(choose.calls)
    assert choose.responses == []
    assert "Choose where to move this file." in choose.last_text
    assert "No archive directories exist yet." in choose.last_text
    chooser = choose.last_buttons
    assert isinstance(chooser, list)
    assert [row[0].text for row in chooser] == ["New directory", "Cancel"]
    session = await queue.latest_interaction_for_actor(1001)
    assert session is not None and session.prompt_message_id == 610

    new_directory = FakeCallback(1001, button_data(chooser[0][0]), message_id=610)
    await bot.handle_callback(new_directory)
    assert new_directory.responses == []
    assert "Send a relative path, for example: videos/2026. Do not start with /." in (
        new_directory.last_text
    )
    awaiting = await queue.get_interaction(session.id)
    assert awaiting is not None
    assert awaiting.step == "await-new-directory"
    assert awaiting.prompt_message_id == 610

    invalid = FakeEvent(sender_id=1001, text="/srv/absolute")
    await bot.handle_event(invalid)
    assert invalid.responses == []
    assert "not a valid relative directory" in client.last_text
    assert "videos/2026. Do not start with /." in client.last_text
    assert await queue.claim_next("worker") is None
    still_awaiting = await queue.get_interaction(session.id)
    assert still_awaiting is not None and still_awaiting.step == "await-new-directory"

    valid = FakeEvent(sender_id=1001, text="videos/2026")
    await bot.handle_event(valid)
    assert valid.responses == []
    assert "Confirm file organization" in client.last_text
    confirmation = client.last_buttons
    assert isinstance(confirmation, list)
    assert [button.text for button in confirmation[0]] == ["Create and move", "Back", "Cancel"]
    refreshed = await queue.get_interaction(session.id)
    assert refreshed is not None and refreshed.prompt_message_id == 610

    back = FakeCallback(1001, button_data(confirmation[0][1]), message_id=610)
    await bot.handle_callback(back)
    assert back.responses == []
    assert "Choose where to move this file." in back.last_text
    back_buttons = back.last_buttons
    assert isinstance(back_buttons, list)

    new_again = FakeCallback(1001, button_data(back_buttons[0][0]), message_id=610)
    await bot.handle_callback(new_again)
    typed_again = FakeEvent(sender_id=1001, text="videos/2026")
    await bot.handle_event(typed_again)
    assert "Create and move" in client.last_text
    final_confirmation = client.last_buttons
    assert isinstance(final_confirmation, list)

    confirm = FakeCallback(1001, button_data(final_confirmation[0][0]), message_id=610)
    await bot.handle_callback(confirm)
    assert confirm.responses == []
    assert len(confirm.edits) >= 2
    assert "Working" in confirm.edits[0]
    assert "Completed" in confirm.last_text
    stored = await queue.get_file(completed.id)
    assert stored is not None
    assert stored.relative_path == Path("archive/videos/2026/clip.mp4")
    assert await queue.latest_interaction_for_actor(1001) is None
    assert str(tmp_path) not in confirm.last_text


async def test_directory_chooser_and_prompt_cancel_edit_the_card(tmp_path: Path) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    await TelegramJobNotifier(client, queue).job_finished(completed)
    card = client.sent[0][2]
    assert isinstance(card, list)
    rename_data = button_data(card[0][1])
    archive_data = button_data(card[1][0])

    choose = FakeCallback(1001, archive_data, message_id=620)
    await bot.handle_callback(choose)
    chooser = choose.last_buttons
    assert isinstance(chooser, list)
    cancel = FakeCallback(1001, button_data(chooser[-1][0]), message_id=620)
    await bot.handle_callback(cancel)
    assert cancel.responses == []
    assert "Action cancelled." in cancel.last_text
    assert await queue.latest_interaction_for_actor(1001) is None

    rename = FakeCallback(1001, rename_data, message_id=621)
    await bot.handle_callback(rename)
    prompt_buttons = rename.last_buttons
    assert isinstance(prompt_buttons, list)
    assert prompt_buttons[0][0].text == "Cancel"
    prompt_cancel = FakeCallback(1001, button_data(prompt_buttons[0][0]), message_id=621)
    await bot.handle_callback(prompt_cancel)
    assert "Action cancelled." in prompt_cancel.last_text
    assert await queue.latest_interaction_for_actor(1001) is None


async def test_active_folder_workflow_routes_slash_text_before_submission(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    await TelegramJobNotifier(client, queue).job_finished(completed)
    card = client.sent[0][2]
    assert isinstance(card, list)
    choose = FakeCallback(1001, button_data(card[1][0]), message_id=630)
    await bot.handle_callback(choose)
    chooser = choose.last_buttons
    assert isinstance(chooser, list)
    new_directory = FakeCallback(1001, button_data(chooser[0][0]), message_id=630)
    await bot.handle_callback(new_directory)
    session = await queue.latest_interaction_for_actor(1001)
    assert session is not None

    unknown = FakeEvent(sender_id=1001, text="/definitely-not-a-command")
    await bot.handle_event(unknown)
    assert unknown.responses == []
    assert "not a valid relative directory" in client.last_text
    assert await queue.claim_next("worker") is None
    kept = await queue.get_interaction(session.id)
    assert kept is not None and kept.step == "await-new-directory"

    status = FakeEvent(sender_id=1001, text="/status")
    await bot.handle_event(status)
    assert status.responses == ["No active jobs."]
    assert await queue.get_interaction(session.id) is not None

    cancel = FakeEvent(sender_id=1001, text="/cancel")
    await bot.handle_event(cancel)
    assert cancel.responses == []
    assert "Action cancelled." in client.last_text
    assert await queue.get_interaction(session.id) is None
    assert await queue.claim_next("worker") is None

    pending = FakeEvent(sender_id=1001, media=object(), file=object(), message_id=92)
    await bot.handle_event(pending)
    job = next(job for job in await queue.latest_for_requester(1001) if job.source_message_id == 92)
    cancel_job = FakeEvent(sender_id=1001, text=f"/cancel {job.id}")
    await bot.handle_event(cancel_job)
    assert "cancelled" in cancel_job.responses[0]


async def test_new_text_workflow_supersedes_only_older_text_workflows(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    first = await completed_file(queue, storage, message_id=500)
    second = await completed_file(queue, storage, message_id=501)
    notifier = TelegramJobNotifier(client, queue)
    await notifier.job_finished(first)
    first_card = client.sent[-1][2]
    assert isinstance(first_card, list)
    await bot.handle_callback(FakeCallback(1001, button_data(first_card[0][1])))
    older = await queue.latest_interaction_for_actor(1001)
    assert older is not None and older.step == "await-name"

    expiry = datetime.now(UTC) + timedelta(minutes=5)
    page = await queue.create_interaction(1001, "files-page", "2", expires_at=expiry)
    ready = await queue.create_interaction(1001, "keep", "ready", expires_at=expiry)

    await notifier.job_finished(second)
    second_card = client.sent[-1][2]
    assert isinstance(second_card, list)
    await bot.handle_callback(FakeCallback(1001, button_data(second_card[1][0])))

    assert await queue.get_interaction(older.id) is None
    assert await queue.get_interaction(page.id) is not None
    assert await queue.get_interaction(ready.id) is not None
    latest = await queue.latest_interaction_for_actor(1001)
    assert latest is not None and latest.step == "await-directory"


async def test_keep_edit_failure_falls_back_to_exactly_one_message(tmp_path: Path) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    session = await queue.create_interaction(
        1001,
        "keep",
        "ready",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
    )
    client.edit_error = MessageIdInvalidError(None)
    event = FakeCallback(
        1001,
        f"tg:{session.id}:keep".encode(),
        message_id=640,
    )

    await bot.handle_callback(event)

    assert event.answers == [""]
    assert event.edits == []
    assert len(event.responses) == 1
    assert "Kept in Inbox" in event.responses[0]
    assert await queue.get_interaction(session.id) is None


async def test_batch_one_card_valid_invalid_back_anchor_auth_and_replay(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    jobs = [
        await completed_file(
            queue,
            storage,
            message_id=970 + index,
            batch_id="batch-one-card",
            batch_expected_size=2,
        )
        for index in range(2)
    ]
    notifier = TelegramJobNotifier(client, queue)
    for job in jobs:
        await notifier.job_finished(job)
    move_all = move_all_buttons(client)
    assert len(move_all) == 1

    opened = FakeCallback(1001, button_data(move_all[0]), message_id=660)
    await bot.handle_callback(opened)
    assert opened.responses == []
    assert "Album · 2 files" in opened.last_text
    session = await queue.latest_interaction_for_actor(1001)
    assert session is not None and session.prompt_message_id == 660
    chooser = opened.last_buttons
    assert isinstance(chooser, list)

    new_directory = FakeCallback(1001, button_data(chooser[0][0]), message_id=660)
    await bot.handle_callback(new_directory)
    assert new_directory.responses == []
    assert "Send a relative path, for example: videos/2026. Do not start with /." in (
        new_directory.last_text
    )

    invalid = FakeEvent(sender_id=1001, text="/absolute/album")
    await bot.handle_event(invalid)
    assert invalid.responses == []
    assert "not a valid relative directory" in client.last_text
    still = await queue.get_interaction(session.id)
    assert still is not None and still.step == "await-new-directory"
    for job in jobs:
        stored = await queue.get_file(job.id)
        assert stored is not None and stored.relative_path.parts[0] == "inbox"

    valid = FakeEvent(sender_id=1001, text="album/2026")
    await bot.handle_event(valid)
    assert "Confirm batch move" in client.last_text
    confirmation = client.last_buttons
    assert isinstance(confirmation, list)
    assert [button.text for button in confirmation[0]] == ["Create and move", "Back", "Cancel"]
    confirm_data = button_data(confirmation[0][0])
    refreshed = await queue.get_interaction(session.id)
    assert refreshed is not None and refreshed.prompt_message_id == 660

    cross_user = FakeCallback(2002, confirm_data, message_id=660)
    await bot.handle_callback(cross_user)
    assert_acknowledged_first(cross_user.calls)
    assert "unauthorized" in cross_user.edits[-1]
    unchanged = await queue.get_interaction(session.id)
    assert unchanged is not None and unchanged.step == "confirm-new-directory"

    back = FakeCallback(1001, button_data(confirmation[0][1]), message_id=660)
    await bot.handle_callback(back)
    assert back.responses == []
    assert "Choose where to move all files" in back.last_text
    back_chooser = back.last_buttons
    assert isinstance(back_chooser, list)

    new_again = FakeCallback(1001, button_data(back_chooser[0][0]), message_id=660)
    await bot.handle_callback(new_again)
    typed_again = FakeEvent(sender_id=1001, text="album/2026")
    await bot.handle_event(typed_again)
    final_confirmation = client.last_buttons
    assert isinstance(final_confirmation, list)
    confirm = FakeCallback(1001, button_data(final_confirmation[0][0]), message_id=660)
    await bot.handle_callback(confirm)

    assert confirm.responses == []
    assert len(confirm.edits) >= 2
    assert "Working" in confirm.edits[0]
    assert "Moved: 2" in confirm.last_text
    assert (storage.archive / "album" / "2026").is_dir()
    for job in jobs:
        stored = await queue.get_file(job.id)
        assert stored is not None
        assert stored.relative_path.parent == Path("archive/album/2026")
    assert await queue.latest_interaction_for_actor(1001) is None

    replay = FakeCallback(1001, confirm_data, message_id=660)
    await bot.handle_callback(replay)
    assert_acknowledged_first(replay.calls)
    assert "stale" in replay.responses[0]
    for job in jobs:
        stored = await queue.get_file(job.id)
        assert stored is not None
        assert stored.relative_path.parent == Path("archive/album/2026")


async def test_single_file_failure_edits_card_and_back_allows_retry(
    tmp_path: Path,
) -> None:
    bot, queue, storage, _ = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    session = await queue.create_interaction(
        1001,
        "archive",
        "confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
        pending_directory="videos",
    )
    calls = {"count": 0}
    original = bot.library.organize

    async def flaky(*args: object, **kwargs: object) -> object:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("private /srv/absolute/path")
        return await original(*args, **kwargs)

    bot.library.organize = flaky  # type: ignore[method-assign]

    confirm = FakeCallback(1001, f"tg:{session.id}:confirm".encode(), message_id=650)
    await bot.handle_callback(confirm)

    assert "Working" in confirm.edits[0]
    assert "library state changed" in confirm.last_text
    assert "/srv/absolute/path" not in confirm.last_text
    retry_controls = confirm.last_buttons
    assert isinstance(retry_controls, list)
    assert [button.text for button in retry_controls[0]] == ["Back", "Cancel"]

    back = FakeCallback(1001, button_data(retry_controls[0][0]), message_id=650)
    await bot.handle_callback(back)
    directories = back.last_buttons
    assert isinstance(directories, list)
    select = FakeCallback(1001, button_data(directories[0][0]), message_id=650)
    await bot.handle_callback(select)
    confirmation = select.last_buttons
    assert isinstance(confirmation, list)
    assert confirmation[0][0].text == "Move here"
    retry = FakeCallback(1001, button_data(confirmation[0][0]), message_id=650)
    await bot.handle_callback(retry)

    stored = await queue.get_file(completed.id)
    assert stored is not None
    assert stored.relative_path == Path("archive/videos/clip.mp4")
    assert "Completed" in retry.last_text
    assert await queue.latest_interaction_for_actor(1001) is None


async def test_multi_stage_confirm_edits_keep_exactly_one_acknowledgement(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    session = await queue.create_interaction(
        1001,
        "archive",
        "confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
        pending_directory="videos",
        prompt_message_id=700,
    )

    event = FakeCallback(1001, f"tg:{session.id}:confirm".encode(), message_id=700)
    await bot.handle_callback(event)

    assert event.answers == [""]
    assert event.implicit_answers == 0
    assert event.responses == []
    assert len(event.edits) == 2
    assert "Working" in event.edits[0]
    assert "Completed" in event.edits[-1]
    assert [edit[1] for edit in client.edited] == [700, 700]


@pytest.mark.parametrize(
    "error",
    [TimeoutError("ambiguous"), MessageTooLongError(None)],
)
async def test_ambiguous_or_oversized_edit_errors_do_not_send_duplicates(
    error: BaseException,
) -> None:
    client = FakeClient(edit_error=error)
    event = FakeCallback(1001, b"", message_id=55)
    feedback = _CallbackFeedback(event, client)

    with pytest.raises(_DeliveryFailure):
        await feedback.deliver("Replacement text", buttons=None)

    assert client.sent == []
    assert client.edited == []
    assert event.implicit_answers == 0


@pytest.mark.parametrize(
    "error",
    [TimeoutError("ambiguous"), MessageTooLongError(None)],
)
async def test_page_non_fallback_failure_keeps_old_page_and_avoids_duplicate_send(
    tmp_path: Path,
    error: BaseException,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    await completed_file(queue, storage)
    session = await queue.create_interaction(
        1001,
        "files-page",
        "2",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        prompt_message_id=90,
    )
    client.edit_error = error
    event = FakeCallback(1001, f"tg:{session.id}:files-page".encode(), message_id=90)

    await bot.handle_callback(event)

    assert event.answers == [""]
    assert event.edits == []
    assert event.responses == ["This action could not be delivered. Please try again."]
    assert len(client.sent) == 1
    restored = await queue.get_interaction(session.id)
    assert restored is not None
    assert restored.action == session.action
    assert restored.step == session.step
    assert restored.prompt_message_id == 90
    assert restored.expires_at == session.expires_at
    rows = await interaction_rows(queue)
    assert [row[0] for row in rows] == [str(session.id)]


async def test_lost_prompt_anchor_cas_disables_the_replacement_card(tmp_path: Path) -> None:
    bot, queue, _storage, client = await full_controller(tmp_path)
    session = await queue.create_interaction(
        1001,
        "rename",
        "await-name",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        prompt_message_id=111,
    )
    winner = await queue.compare_and_set_prompt_message_id(
        session.id,
        1001,
        expected_prompt_message_id=111,
        prompt_message_id=222,
    )
    assert winner is not None

    await bot._bind_prompt(session, 1001, 333, expected=111)

    stored = await queue.get_interaction(session.id)
    assert stored is not None and stored.prompt_message_id == 222
    assert client.edited[-1] == (1001, 333, "This prompt is stale.", None)


async def test_callback_from_older_anchor_is_stale_without_mutation(tmp_path: Path) -> None:
    bot, queue, storage, _client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    session = await queue.create_interaction(
        1001,
        "archive",
        "confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
        pending_directory="videos",
        prompt_message_id=500,
    )
    old = FakeCallback(1001, f"tg:{session.id}:confirm".encode(), message_id=499)

    await bot.handle_callback(old)

    assert old.answers == [""]
    assert old.edits == []
    assert "older card" in old.responses[0]
    unchanged = await queue.get_interaction(session.id)
    assert unchanged is not None and unchanged.step == "confirm"

    current = FakeCallback(1001, f"tg:{session.id}:confirm".encode(), message_id=500)
    await bot.handle_callback(current)
    stored = await queue.get_file(completed.id)
    assert stored is not None
    assert stored.relative_path == Path("archive/videos/clip.mp4")
    assert "Completed" in current.edits[-1]


async def test_concurrent_activation_of_two_cards_leaves_one_live_workflow(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    first = await completed_file(queue, storage, message_id=1100)
    second = await completed_file(queue, storage, message_id=1101)
    notifier = TelegramJobNotifier(client, queue)
    await notifier.job_finished(first)
    first_card = client.sent[-1][2]
    await notifier.job_finished(second)
    second_card = client.sent[-1][2]
    assert isinstance(first_card, list) and isinstance(second_card, list)
    expiry = datetime.now(UTC) + timedelta(minutes=5)
    page = await queue.create_interaction(1001, "files-page", "2", expires_at=expiry)
    ready = await queue.create_interaction(1001, "keep", "ready", expires_at=expiry)

    await asyncio.gather(
        bot.handle_callback(FakeCallback(1001, button_data(first_card[0][1]), message_id=800)),
        bot.handle_callback(FakeCallback(1001, button_data(second_card[1][0]), message_id=801)),
    )

    async with aiosqlite.connect(queue.path) as connection:
        active = await (
            await connection.execute(
                """
                SELECT id FROM interactions
                WHERE actor_id = 1001
                  AND action IN ('rename', 'archive', 'rename-and-archive', 'batch-move')
                  AND step IN (
                      'await-name', 'await-directory', 'await-new-directory',
                      'confirm', 'confirm-new-directory'
                  )
                """
            )
        ).fetchall()
    assert len(active) == 1
    assert await queue.get_interaction(page.id) is not None
    assert await queue.get_interaction(ready.id) is not None


async def test_concurrent_single_confirmation_runs_one_operation(tmp_path: Path) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    session = await queue.create_interaction(
        1001,
        "archive",
        "confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
        pending_directory="videos",
        prompt_message_id=900,
    )
    started = asyncio.Event()
    release = asyncio.Event()
    calls = {"count": 0}
    original = bot.library.organize

    async def blocking(*args: object, **kwargs: object) -> object:
        calls["count"] += 1
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    bot.library.organize = blocking  # type: ignore[method-assign]
    winner = FakeCallback(1001, f"tg:{session.id}:confirm".encode(), message_id=900)
    task = asyncio.create_task(bot.handle_callback(winner))
    await started.wait()
    loser = FakeCallback(1001, f"tg:{session.id}:confirm".encode(), message_id=900)

    await bot.handle_callback(loser)

    assert loser.answers == [""]
    assert loser.edits == []
    assert loser.responses == ["This action is already being processed."]
    assert calls["count"] == 1
    assert await queue.get_interaction(session.id) is not None

    release.set()
    await task

    assert winner.answers == [""]
    assert winner.implicit_answers == 0
    assert "Completed" in client.edited[-1][2]
    stored = await queue.get_file(completed.id)
    assert stored is not None
    assert stored.relative_path == Path("archive/videos/clip.mp4")
    assert await queue.get_interaction(session.id) is None


async def test_concurrent_batch_confirmation_runs_one_operation(tmp_path: Path) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    jobs = [
        await completed_file(
            queue,
            storage,
            message_id=1200 + index,
            batch_id="batch-confirm-race",
            batch_expected_size=2,
        )
        for index in range(2)
    ]
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    session = await queue.create_interaction(
        1001,
        "batch-move",
        "confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        batch_id="batch-confirm-race",
        pending_directory="videos",
        prompt_message_id=901,
    )
    started = asyncio.Event()
    release = asyncio.Event()
    calls = {"count": 0}
    original = bot.library.organize_batch

    async def blocking(*args: object, **kwargs: object) -> object:
        calls["count"] += 1
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    bot.library.organize_batch = blocking  # type: ignore[method-assign]
    winner = FakeCallback(1001, f"tg:{session.id}:confirm".encode(), message_id=901)
    task = asyncio.create_task(bot.handle_callback(winner))
    await started.wait()
    loser = FakeCallback(1001, f"tg:{session.id}:confirm".encode(), message_id=901)

    await bot.handle_callback(loser)

    assert loser.edits == []
    assert loser.responses == ["This action is already being processed."]
    assert calls["count"] == 1

    release.set()
    await task

    assert winner.answers == [""]
    assert "Moved: 2" in client.edited[-1][2]
    for job in jobs:
        stored = await queue.get_file(job.id)
        assert stored is not None
        assert stored.relative_path.parent == Path("archive/videos")
    assert await queue.get_interaction(session.id) is None


async def test_single_new_directory_failure_keeps_back_cancel_and_recovers(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    session = await queue.create_interaction(
        1001,
        "archive",
        "confirm-new-directory",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
        pending_directory="videos/2026",
        prompt_message_id=950,
    )
    original_mkdir = bot.library.mkdir

    async def fail_once(*args: object, **kwargs: object) -> object:
        bot.library.mkdir = original_mkdir  # type: ignore[method-assign]
        raise OSError("private path detail")

    bot.library.mkdir = fail_once  # type: ignore[method-assign]
    confirm = FakeCallback(1001, f"tg:{session.id}:confirm".encode(), message_id=950)

    await bot.handle_callback(confirm)

    assert "directory could not be created" in confirm.last_text
    controls = confirm.last_buttons
    assert isinstance(controls, list)
    assert [button.text for button in controls[0]] == ["Back", "Cancel"]
    restored = await queue.get_interaction(session.id)
    assert restored is not None and restored.step == "confirm-new-directory"

    back = FakeCallback(1001, button_data(controls[0][0]), message_id=950)
    await bot.handle_callback(back)
    directories = back.last_buttons
    assert isinstance(directories, list)
    select = FakeCallback(1001, button_data(directories[0][0]), message_id=950)
    await bot.handle_callback(select)
    retry_controls = select.last_buttons
    assert isinstance(retry_controls, list)
    retry = FakeCallback(1001, button_data(retry_controls[0][0]), message_id=950)
    await bot.handle_callback(retry)

    stored = await queue.get_file(completed.id)
    assert stored is not None
    assert stored.relative_path == Path("archive/videos/clip.mp4")
    assert "Completed" in client.edited[-1][2]


async def test_single_stale_file_consumes_the_interaction_without_controls(
    tmp_path: Path,
) -> None:
    bot, queue, storage, _client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    session = await queue.create_interaction(
        1001,
        "archive",
        "confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
        pending_directory="videos",
        prompt_message_id=960,
    )

    async def missing(*args: object, **kwargs: object) -> object:
        raise StoredFileNotFoundError("gone")

    assert bot.library is not None
    bot.library.resolve = missing  # type: ignore[method-assign]
    event = FakeCallback(1001, f"tg:{session.id}:confirm".encode(), message_id=960)

    await bot.handle_callback(event)

    assert "no longer available" in event.edits[-1]
    assert event.last_buttons is None
    assert await queue.get_interaction(session.id) is None


async def test_batch_new_directory_failure_keeps_back_cancel_and_recovers(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    jobs = [
        await completed_file(
            queue,
            storage,
            message_id=1300 + index,
            batch_id="batch-mkdir-failure",
            batch_expected_size=2,
        )
        for index in range(2)
    ]
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    session = await queue.create_interaction(
        1001,
        "batch-move",
        "confirm-new-directory",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        batch_id="batch-mkdir-failure",
        pending_directory="videos/new",
        prompt_message_id=970,
    )
    original_mkdir = bot.library.mkdir

    async def fail_once(*args: object, **kwargs: object) -> object:
        bot.library.mkdir = original_mkdir  # type: ignore[method-assign]
        raise OSError("private path detail")

    bot.library.mkdir = fail_once  # type: ignore[method-assign]
    confirm = FakeCallback(1001, f"tg:{session.id}:confirm".encode(), message_id=970)

    await bot.handle_callback(confirm)

    assert "directory could not be created" in confirm.last_text
    controls = confirm.last_buttons
    assert isinstance(controls, list)
    assert [button.text for button in controls[0]] == ["Back", "Cancel"]
    restored = await queue.get_interaction(session.id)
    assert restored is not None and restored.step == "confirm-new-directory"

    back = FakeCallback(1001, button_data(controls[0][0]), message_id=970)
    await bot.handle_callback(back)
    directories = back.last_buttons
    assert isinstance(directories, list)
    select = FakeCallback(1001, button_data(directories[0][0]), message_id=970)
    await bot.handle_callback(select)
    retry_controls = select.last_buttons
    assert isinstance(retry_controls, list)
    retry = FakeCallback(1001, button_data(retry_controls[0][0]), message_id=970)
    await bot.handle_callback(retry)

    for job in jobs:
        stored = await queue.get_file(job.id)
        assert stored is not None
        assert stored.relative_path.parent == Path("archive/videos")
    assert "Moved: 2" in client.edited[-1][2]


async def test_batch_validation_failure_keeps_back_cancel_and_recovers(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    jobs = [
        await completed_file(
            queue,
            storage,
            message_id=1400 + index,
            batch_id="batch-validation",
            batch_expected_size=2,
        )
        for index in range(2)
    ]
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    session = await queue.create_interaction(
        1001,
        "batch-move",
        "confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        batch_id="batch-validation",
        pending_directory="../outside",
        prompt_message_id=980,
    )
    confirm = FakeCallback(1001, f"tg:{session.id}:confirm".encode(), message_id=980)

    await bot.handle_callback(confirm)

    assert "name or directory is invalid" in confirm.last_text
    controls = confirm.last_buttons
    assert isinstance(controls, list)
    assert [button.text for button in controls[0]] == ["Back", "Cancel"]
    restored = await queue.get_interaction(session.id)
    assert restored is not None and restored.step == "confirm"

    back = FakeCallback(1001, button_data(controls[0][0]), message_id=980)
    await bot.handle_callback(back)
    directories = back.last_buttons
    assert isinstance(directories, list)
    select = FakeCallback(1001, button_data(directories[0][0]), message_id=980)
    await bot.handle_callback(select)
    retry_controls = select.last_buttons
    assert isinstance(retry_controls, list)
    retry = FakeCallback(1001, button_data(retry_controls[0][0]), message_id=980)
    await bot.handle_callback(retry)

    for job in jobs:
        stored = await queue.get_file(job.id)
        assert stored is not None
        assert stored.relative_path.parent == Path("archive/videos")
    assert "Moved: 2" in client.edited[-1][2]


async def test_batch_runtime_failure_offers_retry_cancel_and_recovers(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    jobs = [
        await completed_file(
            queue,
            storage,
            message_id=1500 + index,
            batch_id="batch-runtime",
            batch_expected_size=2,
        )
        for index in range(2)
    ]
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    session = await queue.create_interaction(
        1001,
        "batch-move",
        "confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        batch_id="batch-runtime",
        pending_directory="videos",
        prompt_message_id=990,
    )
    original = bot.library.organize_batch

    async def fail_once(*args: object, **kwargs: object) -> object:
        bot.library.organize_batch = original  # type: ignore[method-assign]
        raise OSError("private runtime detail")

    bot.library.organize_batch = fail_once  # type: ignore[method-assign]
    confirm = FakeCallback(1001, f"tg:{session.id}:confirm".encode(), message_id=990)

    await bot.handle_callback(confirm)

    assert "library state changed" in confirm.last_text
    controls = confirm.last_buttons
    assert isinstance(controls, list)
    assert [button.text for button in controls[0]] == ["Retry move", "Cancel"]
    restored = await queue.get_interaction(session.id)
    assert restored is not None and restored.step == "ready"

    retry = FakeCallback(1001, button_data(controls[0][0]), message_id=990)
    await bot.handle_callback(retry)
    directories = retry.last_buttons
    assert isinstance(directories, list)
    select = FakeCallback(1001, button_data(directories[0][0]), message_id=990)
    await bot.handle_callback(select)
    retry_controls = select.last_buttons
    assert isinstance(retry_controls, list)
    final = FakeCallback(1001, button_data(retry_controls[0][0]), message_id=990)
    await bot.handle_callback(final)

    for job in jobs:
        stored = await queue.get_file(job.id)
        assert stored is not None
        assert stored.relative_path.parent == Path("archive/videos")
    assert "Moved: 2" in client.edited[-1][2]


async def test_batch_not_found_consumes_interaction_without_stale_controls(
    tmp_path: Path,
) -> None:
    bot, queue, storage, _client = await full_controller(tmp_path)
    await completed_file(
        queue,
        storage,
        message_id=1600,
        batch_id="batch-missing",
        batch_expected_size=2,
    )
    session = await queue.create_interaction(
        1001,
        "batch-move",
        "confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        batch_id="batch-missing",
        pending_directory="videos",
        prompt_message_id=995,
    )

    async def missing(*args: object, **kwargs: object) -> object:
        raise BatchNotFoundError("missing")

    assert bot.library is not None
    bot.library.organize_batch = missing  # type: ignore[method-assign]
    event = FakeCallback(1001, f"tg:{session.id}:confirm".encode(), message_id=995)

    await bot.handle_callback(event)

    assert "no longer available" in event.edits[-1]
    assert event.last_buttons is None
    assert await queue.get_interaction(session.id) is None


async def test_single_validation_fallback_rebinds_anchor_and_back_works(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    session = await queue.create_interaction(
        1001,
        "archive",
        "confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
        pending_directory="../outside",
        prompt_message_id=950,
    )
    client.edit_error_sequence = [MessageIdInvalidError(None)]
    confirm = FakeCallback(1001, f"tg:{session.id}:confirm".encode(), message_id=950)

    await bot.handle_callback(confirm)

    replacement_id = client.sent_ids[-1]
    replacement_buttons = client.sent[-1][2]
    assert isinstance(replacement_buttons, list)
    assert "name or directory is invalid" in client.sent[-1][1]
    stored = await queue.get_interaction(session.id)
    assert stored is not None and stored.step == "confirm"
    assert stored.prompt_message_id == replacement_id

    back = FakeCallback(1001, button_data(replacement_buttons[0][0]), message_id=replacement_id)
    await bot.handle_callback(back)

    assert "Choose where" in client.edited[-1][2]
    assert client.edited[-1][1] == replacement_id
    updated = await queue.get_interaction(session.id)
    assert updated is not None and updated.step == "await-directory"
    assert updated.prompt_message_id == replacement_id


async def test_single_mkdir_fallback_rebinds_anchor_and_back_works(tmp_path: Path) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    session = await queue.create_interaction(
        1001,
        "archive",
        "confirm-new-directory",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
        pending_directory="videos/2026",
        prompt_message_id=951,
    )

    async def fail_mkdir(*args: object, **kwargs: object) -> object:
        raise OSError("private path detail")

    assert bot.library is not None
    bot.library.mkdir = fail_mkdir  # type: ignore[method-assign]
    client.edit_error_sequence = [None, MessageIdInvalidError(None)]
    confirm = FakeCallback(1001, f"tg:{session.id}:confirm".encode(), message_id=951)

    await bot.handle_callback(confirm)

    replacement_id = client.sent_ids[-1]
    replacement_buttons = client.sent[-1][2]
    assert isinstance(replacement_buttons, list)
    assert [button.text for button in replacement_buttons[0]] == ["Back", "Cancel"]
    stored = await queue.get_interaction(session.id)
    assert stored is not None and stored.step == "confirm-new-directory"
    assert stored.prompt_message_id == replacement_id

    back = FakeCallback(1001, button_data(replacement_buttons[0][0]), message_id=replacement_id)
    await bot.handle_callback(back)
    assert "Choose where" in client.edited[-1][2]
    updated = await queue.get_interaction(session.id)
    assert updated is not None and updated.step == "await-directory"


async def test_single_runtime_fallback_rebinds_working_anchor_and_back_works(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    session = await queue.create_interaction(
        1001,
        "archive",
        "confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
        pending_directory="videos",
        prompt_message_id=952,
    )

    async def fail_organize(*args: object, **kwargs: object) -> object:
        raise OSError("private runtime detail")

    bot.library.organize = fail_organize  # type: ignore[method-assign]
    client.edit_error_sequence = [None, MessageIdInvalidError(None)]
    confirm = FakeCallback(1001, f"tg:{session.id}:confirm".encode(), message_id=952)

    await bot.handle_callback(confirm)

    replacement_id = client.sent_ids[-1]
    replacement_buttons = client.sent[-1][2]
    assert isinstance(replacement_buttons, list)
    assert [button.text for button in replacement_buttons[0]] == ["Back", "Cancel"]
    stored = await queue.get_interaction(session.id)
    assert stored is not None and stored.step == "confirm"
    assert stored.prompt_message_id == replacement_id

    back = FakeCallback(1001, button_data(replacement_buttons[0][0]), message_id=replacement_id)
    await bot.handle_callback(back)
    assert "Choose where" in client.edited[-1][2]
    updated = await queue.get_interaction(session.id)
    assert updated is not None and updated.step == "await-directory"


async def test_batch_validation_fallback_rebinds_anchor_and_back_works(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    for index in range(2):
        await completed_file(
            queue,
            storage,
            message_id=1700 + index,
            batch_id="batch-validation-fallback",
            batch_expected_size=2,
        )
    session = await queue.create_interaction(
        1001,
        "batch-move",
        "confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        batch_id="batch-validation-fallback",
        pending_directory="../outside",
        prompt_message_id=953,
    )
    client.edit_error_sequence = [MessageIdInvalidError(None)]
    confirm = FakeCallback(1001, f"tg:{session.id}:confirm".encode(), message_id=953)

    await bot.handle_callback(confirm)

    replacement_id = client.sent_ids[-1]
    replacement_buttons = client.sent[-1][2]
    assert isinstance(replacement_buttons, list)
    assert "name or directory is invalid" in client.sent[-1][1]
    stored = await queue.get_interaction(session.id)
    assert stored is not None and stored.step == "confirm"
    assert stored.prompt_message_id == replacement_id

    back = FakeCallback(1001, button_data(replacement_buttons[0][0]), message_id=replacement_id)
    await bot.handle_callback(back)
    assert "Choose where" in client.edited[-1][2]
    updated = await queue.get_interaction(session.id)
    assert updated is not None and updated.step == "await-directory"


async def test_batch_mkdir_fallback_rebinds_anchor_and_back_works(tmp_path: Path) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    for index in range(2):
        await completed_file(
            queue,
            storage,
            message_id=1800 + index,
            batch_id="batch-mkdir-fallback",
            batch_expected_size=2,
        )
    session = await queue.create_interaction(
        1001,
        "batch-move",
        "confirm-new-directory",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        batch_id="batch-mkdir-fallback",
        pending_directory="videos/new",
        prompt_message_id=954,
    )

    async def fail_mkdir(*args: object, **kwargs: object) -> object:
        raise OSError("private path detail")

    assert bot.library is not None
    bot.library.mkdir = fail_mkdir  # type: ignore[method-assign]
    client.edit_error_sequence = [None, MessageIdInvalidError(None)]
    confirm = FakeCallback(1001, f"tg:{session.id}:confirm".encode(), message_id=954)

    await bot.handle_callback(confirm)

    replacement_id = client.sent_ids[-1]
    replacement_buttons = client.sent[-1][2]
    assert isinstance(replacement_buttons, list)
    assert [button.text for button in replacement_buttons[0]] == ["Back", "Cancel"]
    stored = await queue.get_interaction(session.id)
    assert stored is not None and stored.step == "confirm-new-directory"
    assert stored.prompt_message_id == replacement_id

    back = FakeCallback(1001, button_data(replacement_buttons[0][0]), message_id=replacement_id)
    await bot.handle_callback(back)
    assert "Choose where" in client.edited[-1][2]
    updated = await queue.get_interaction(session.id)
    assert updated is not None and updated.step == "await-directory"


async def test_batch_partial_retry_fallback_rebinds_anchor_and_retry_works(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    jobs = [
        await completed_file(
            queue,
            storage,
            message_id=1900 + index,
            batch_id="batch-partial-fallback",
            batch_expected_size=2,
        )
        for index in range(2)
    ]
    session = await queue.create_interaction(
        1001,
        "batch-move",
        "confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        batch_id="batch-partial-fallback",
        pending_directory="videos",
        prompt_message_id=955,
    )
    partial = BatchMoveResult(
        batch_id="batch-partial-fallback",
        total_members=2,
        moved=1,
        already_moved=0,
        missing=0,
        not_ready=1,
        failed=0,
        items=(
            BatchMoveItem(jobs[0].id, BatchMoveCategory.MOVED),
            BatchMoveItem(jobs[1].id, BatchMoveCategory.NOT_READY),
        ),
    )

    async def partial_move(*args: object, **kwargs: object) -> object:
        return partial

    assert bot.library is not None
    bot.library.organize_batch = partial_move  # type: ignore[method-assign]
    client.edit_error_sequence = [None, MessageIdInvalidError(None)]
    confirm = FakeCallback(1001, f"tg:{session.id}:confirm".encode(), message_id=955)

    await bot.handle_callback(confirm)

    replacement_id = client.sent_ids[-1]
    replacement_buttons = client.sent[-1][2]
    assert isinstance(replacement_buttons, list)
    assert [button.text for button in replacement_buttons[0]] == ["Retry move", "Cancel"]
    assert "Some files were not moved" in client.sent[-1][1]
    stored = await queue.get_interaction(session.id)
    assert stored is not None and stored.step == "ready"
    assert stored.prompt_message_id == replacement_id

    retry = FakeCallback(1001, button_data(replacement_buttons[0][0]), message_id=replacement_id)
    await bot.handle_callback(retry)
    assert "Choose where" in client.edited[-1][2]
    updated = await queue.get_interaction(session.id)
    assert updated is not None and updated.step == "await-directory"
    assert updated.prompt_message_id == replacement_id


async def test_concurrent_page_callback_has_one_editor_and_one_successor_set(
    tmp_path: Path,
) -> None:
    bot, queue, _storage, client = await full_controller(tmp_path)
    session = await queue.create_interaction(
        1001,
        "history-page",
        "3",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        prompt_message_id=700,
    )
    payload = f"tg:{session.id}:history-page".encode()
    first = FakeCallback(1001, payload, message_id=700)
    second = FakeCallback(1001, payload, message_id=700)

    await asyncio.gather(bot.handle_callback(first), bot.handle_callback(second))

    assert len(client.edited) == 1
    assert client.edited[0][1] == 700
    assert "No history on page 3." in client.edited[0][2]
    assert await queue.get_interaction(session.id) is None
    rows = await interaction_rows(queue)
    assert len(rows) == 1
    assert rows[0][1] == 700
    assert len(client.sent) == 1
    assert client.sent[0][1] == "This action is stale or invalid."


async def test_page_successor_click_and_sequential_replay_are_safe(tmp_path: Path) -> None:
    bot, queue, _storage, client = await full_controller(tmp_path)
    session = await queue.create_interaction(
        1001,
        "history-page",
        "3",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        prompt_message_id=701,
    )
    first = FakeCallback(1001, f"tg:{session.id}:history-page".encode(), message_id=701)

    await bot.handle_callback(first)

    assert await queue.get_interaction(session.id) is None
    page_buttons = client.edited[-1][3]
    assert isinstance(page_buttons, list)
    successor_data = button_data(page_buttons[0][0])
    replay = FakeCallback(1001, f"tg:{session.id}:history-page".encode(), message_id=701)
    await bot.handle_callback(replay)
    assert replay.responses == ["This action is unauthorized, expired, or stale."]
    assert len(client.edited) == 1

    successor = FakeCallback(1001, successor_data, message_id=701)
    await bot.handle_callback(successor)
    assert "No history on page 2." in client.edited[-1][2]
    assert client.edited[-1][1] == 701
    assert len(client.edited) == 2


async def test_page_cancellation_during_edit_commits_visible_successors(
    tmp_path: Path,
) -> None:
    bot, queue, _storage, client = await full_controller(tmp_path)
    session = await queue.create_interaction(
        1001,
        "history-page",
        "3",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        prompt_message_id=702,
    )
    client.edit_gate_text = "No history on page 3."
    event = FakeCallback(1001, f"tg:{session.id}:history-page".encode(), message_id=702)
    task = asyncio.create_task(bot.handle_callback(event))
    await asyncio.wait_for(client.edit_gate_entered.wait(), timeout=2)

    task.cancel()
    await asyncio.sleep(0)
    client.edit_gate_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The shielded edit completed, so the visible card must keep a live token
    # instead of being rolled back to an interaction that is no longer shown.
    assert await queue.get_interaction(session.id) is None
    buttons = client.edited[-1][3]
    assert isinstance(buttons, list)
    successor = await queue.get_interaction(
        UUID(button_data(buttons[0][0]).decode().split(":", 2)[1])
    )
    assert successor is not None
    assert successor.prompt_message_id == 702
    rows = await interaction_rows(queue)
    assert [row[0] for row in rows] == [str(successor.id)]
    assert rows[0][1] == 702


async def test_single_working_delivery_cancellation_restores_confirmation(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    session = await queue.create_interaction(
        1001,
        "archive",
        "confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
        pending_directory="videos",
        prompt_message_id=900,
    )
    client.edit_gate_text = "Working… Moving the file."
    event = FakeCallback(1001, f"tg:{session.id}:confirm".encode(), message_id=900)
    task = asyncio.create_task(bot.handle_callback(event))
    await asyncio.wait_for(client.edit_gate_entered.wait(), timeout=2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    restored = await queue.get_interaction(session.id)
    assert restored is not None and restored.step == "confirm"
    assert restored.prompt_message_id == 900
    assert restored.pending_directory == "videos"


async def test_single_operation_cancellation_settles_pending_result(tmp_path: Path) -> None:
    bot, queue, storage, _client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    session = await queue.create_interaction(
        1001,
        "archive",
        "confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
        pending_directory="videos",
        prompt_message_id=901,
    )
    started = asyncio.Event()
    release = asyncio.Event()
    original = bot.library.organize

    async def blocking(*args: object, **kwargs: object) -> object:
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    bot.library.organize = blocking  # type: ignore[method-assign]
    event = FakeCallback(1001, f"tg:{session.id}:confirm".encode(), message_id=901)
    task = asyncio.create_task(bot.handle_callback(event))
    await asyncio.wait_for(started.wait(), timeout=2)

    # The library operation and pending transition are shielded together, so
    # the committed result settles before the outer cancellation propagates.
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    pending = await queue.get_interaction(session.id)
    assert pending is not None and pending.step == "result-pending"
    assert pending.prompt_message_id == 901
    stored = await queue.get_file(completed.id)
    assert stored is not None
    assert stored.relative_path == Path("archive/videos/clip.mp4")

    # Recovery delivers the committed result once and never replays the move.
    assert await bot.restore_pending_results(await pending_sessions(queue)) == 1
    assert await queue.get_interaction(session.id) is None
    async with aiosqlite.connect(queue.path) as connection:
        count = await (
            await connection.execute(
                "SELECT COUNT(*) FROM file_audit WHERE job_id = ?",
                (str(completed.id),),
            )
        ).fetchone()
    assert count == (1,)


async def test_single_result_delivery_cancellation_settles_pending_result(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    session = await queue.create_interaction(
        1001,
        "archive",
        "confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
        pending_directory="videos",
        prompt_message_id=902,
    )
    client.edit_gate_text = "Completed:"
    event = FakeCallback(1001, f"tg:{session.id}:confirm".encode(), message_id=902)
    task = asyncio.create_task(bot.handle_callback(event))
    await asyncio.wait_for(client.edit_gate_entered.wait(), timeout=2)

    task.cancel()
    await asyncio.sleep(0)
    # The terminal result delivery is shielded: it must finish so the known
    # success can remove the pending row instead of leaving lost controls.
    client.edit_gate_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await queue.get_interaction(session.id) is None
    stored = await queue.get_file(completed.id)
    assert stored is not None
    assert stored.relative_path == Path("archive/videos/clip.mp4")


async def test_batch_operation_cancellation_settles_pending_result(tmp_path: Path) -> None:
    bot, queue, storage, _client = await full_controller(tmp_path)
    jobs = [
        await completed_file(
            queue,
            storage,
            message_id=2000 + index,
            batch_id="batch-cancel-operation",
            batch_expected_size=2,
        )
        for index in range(2)
    ]
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    session = await queue.create_interaction(
        1001,
        "batch-move",
        "confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        batch_id="batch-cancel-operation",
        pending_directory="videos",
        prompt_message_id=903,
    )
    started = asyncio.Event()
    release = asyncio.Event()
    original = bot.library.organize_batch

    async def blocking(*args: object, **kwargs: object) -> object:
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    bot.library.organize_batch = blocking  # type: ignore[method-assign]
    event = FakeCallback(1001, f"tg:{session.id}:confirm".encode(), message_id=903)
    task = asyncio.create_task(bot.handle_callback(event))
    await asyncio.wait_for(started.wait(), timeout=2)

    # The batch and its pending transition are shielded together.
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    pending = await queue.get_interaction(session.id)
    assert pending is not None and pending.step == "result-pending"
    assert pending.prompt_message_id == 903
    for job in jobs:
        item = await queue.get_file(job.id)
        assert item is not None
        assert item.relative_path.parent == Path("archive/videos")

    assert await bot.restore_pending_results(await pending_sessions(queue)) == 1
    assert await queue.get_interaction(session.id) is None
    async with aiosqlite.connect(queue.path) as connection:
        rows = await (
            await connection.execute("SELECT job_id, COUNT(*) FROM file_audit GROUP BY job_id")
        ).fetchall()
    counts = {row[0]: row[1] for row in rows if row[0] is not None}
    assert counts == {str(job.id): 1 for job in jobs}


async def test_page_edit_same_message_needs_no_rebind(
    tmp_path: Path,
) -> None:
    bot, queue, _storage, client = await full_controller(tmp_path)
    session = await queue.create_interaction(
        1001,
        "history-page",
        "3",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        prompt_message_id=710,
    )
    calls: list[object] = []
    original = queue.compare_and_set_prompt_message_id

    async def should_not_rebind(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        raise RuntimeError("same-message edits must not rebind")

    queue.compare_and_set_prompt_message_id = should_not_rebind  # type: ignore[method-assign]
    try:
        event = FakeCallback(1001, f"tg:{session.id}:history-page".encode(), message_id=710)
        await bot.handle_callback(event)
    finally:
        queue.compare_and_set_prompt_message_id = original  # type: ignore[method-assign]

    assert calls == []
    assert await queue.get_interaction(session.id) is None
    buttons = client.edited[-1][3]
    assert isinstance(buttons, list)
    successor = await queue.get_interaction(
        UUID(button_data(buttons[0][0]).decode().split(":", 2)[1])
    )
    assert successor is not None
    assert successor.prompt_message_id == 710
    assert [tuple(row) for row in await interaction_rows(queue)] == [(str(successor.id), 710)]


async def test_page_fallback_cancellation_during_rebind_keeps_live_successors(
    tmp_path: Path,
) -> None:
    bot, queue, _storage, client = await full_controller(tmp_path)
    session = await queue.create_interaction(
        1001,
        "history-page",
        "3",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        prompt_message_id=711,
    )
    client.edit_error_sequence = [MessageIdInvalidError(None)]
    entered = asyncio.Event()
    release = asyncio.Event()
    original = queue.compare_and_set_prompt_message_id

    async def gated_rebind(*args: object, **kwargs: object) -> object:
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    queue.compare_and_set_prompt_message_id = gated_rebind  # type: ignore[method-assign]
    event = FakeCallback(1001, f"tg:{session.id}:history-page".encode(), message_id=711)
    task = asyncio.create_task(bot.handle_callback(event))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        queue.compare_and_set_prompt_message_id = original  # type: ignore[method-assign]

    replacement_id = client.sent_ids[-1]
    assert replacement_id != 711
    buttons = client.sent[-1][2]
    assert isinstance(buttons, list)
    successor = await queue.get_interaction(
        UUID(button_data(buttons[0][0]).decode().split(":", 2)[1])
    )
    assert successor is not None
    assert successor.prompt_message_id == replacement_id
    assert [tuple(row) for row in await interaction_rows(queue)] == [
        (str(successor.id), replacement_id)
    ]
    # The replacement card's visible token is backed by its persisted row.
    click = FakeCallback(1001, button_data(buttons[0][0]), message_id=replacement_id)
    await bot.handle_callback(click)
    assert "No history on page 2." in click.edits[-1]


async def test_page_fallback_rebind_failure_restores_visible_source_token(
    tmp_path: Path,
) -> None:
    bot, queue, _storage, client = await full_controller(tmp_path)
    session = await queue.create_interaction(
        1001,
        "history-page",
        "3",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        prompt_message_id=712,
    )
    client.edit_error_sequence = [MessageIdInvalidError(None)]
    original = queue.compare_and_set_prompt_message_id

    async def broken_binding(*args: object, **kwargs: object) -> object:
        raise RuntimeError("repository detail")

    queue.compare_and_set_prompt_message_id = broken_binding  # type: ignore[method-assign]
    event = FakeCallback(1001, f"tg:{session.id}:history-page".encode(), message_id=712)
    event.message = SimpleNamespace(text="History · page 2:\nNo history on page 2.")
    try:
        await bot.handle_callback(event)
    finally:
        queue.compare_and_set_prompt_message_id = original  # type: ignore[method-assign]

    replacement_id = client.sent_ids[-1]
    assert replacement_id != 712
    restored = await queue.get_interaction(session.id)
    assert restored is not None
    assert restored.action == session.action
    assert restored.step == session.step
    assert restored.prompt_message_id == 712
    assert [tuple(row) for row in await interaction_rows(queue)] == [(str(session.id), 712)]
    # The unbound replacement is disabled and the restored source card is
    # re-rendered with a token that resolves to the live source row.
    assert (1001, replacement_id, "This prompt is stale.", None) in client.edited
    source_card = client.edited[-1]
    assert source_card[1] == 712
    assert source_card[2] == "History · page 2:\nNo history on page 2."
    buttons = source_card[3]
    assert isinstance(buttons, list)
    assert button_data(buttons[0][0]).decode().endswith(f":{session.id}:history-page")
    assert "could not be delivered" not in event.responses


async def test_restored_working_card_has_clickable_confirmation(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    session = await queue.create_interaction(
        1001,
        "archive",
        "working-confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
        pending_name="clip.mp4",
        pending_directory="videos",
        prompt_message_id=720,
    )

    recovered = await queue.recover_working_interactions(
        expires_at=datetime.now(UTC) + timedelta(hours=1)
    )
    assert [item.id for item in recovered] == [session.id]
    assert await bot.restore_interaction_cards(recovered) == 1

    actor_id, message_id, text, buttons = client.edited[-1]
    assert actor_id == 1001
    assert message_id == 720
    assert "Confirm file organization:" in text
    assert "Name: clip.mp4" in text
    assert "Directory: videos" in text
    assert isinstance(buttons, list)
    assert [button.text for button in buttons[0]] == ["Move here", "Back", "Cancel"]
    confirm = FakeCallback(1001, button_data(buttons[0][0]), message_id=720)
    await bot.handle_callback(confirm)
    stored = await queue.get_file(completed.id)
    assert stored is not None
    assert stored.relative_path == Path("archive/videos/clip.mp4")
    assert "Completed" in confirm.edits[-1]


async def test_restored_legacy_working_card_offers_choose_folder(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    session = await queue.create_interaction(
        1001,
        "archive",
        "working",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
        pending_directory="videos",
        prompt_message_id=721,
    )

    recovered = await queue.recover_working_interactions(
        expires_at=datetime.now(UTC) + timedelta(hours=1)
    )
    assert await bot.restore_interaction_cards(recovered) == 1

    _, message_id, text, buttons = client.edited[-1]
    assert message_id == 721
    assert "interrupted" in text
    assert isinstance(buttons, list)
    assert [button.text for button in buttons[0]] == ["Choose folder", "Cancel"]
    choose = FakeCallback(1001, button_data(buttons[0][0]), message_id=721)
    await bot.handle_callback(choose)
    assert "Choose where to move this file." in choose.edits[-1]
    assert "videos" in choose.edits[-1]
    updated = await queue.get_interaction(session.id)
    assert updated is not None and updated.step == "await-directory"


async def test_restored_working_card_fallback_binds_cas_anchor(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    session = await queue.create_interaction(
        1001,
        "archive",
        "working-confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
        pending_directory="videos",
        prompt_message_id=722,
    )
    client.edit_error_sequence = [MessageIdInvalidError(None)]

    recovered = await queue.recover_working_interactions(
        expires_at=datetime.now(UTC) + timedelta(hours=1)
    )
    assert await bot.restore_interaction_cards(recovered) == 1

    replacement_id = client.sent_ids[-1]
    assert replacement_id != 722
    restored = await queue.get_interaction(session.id)
    assert restored is not None
    assert restored.step == "confirm"
    assert restored.prompt_message_id == replacement_id
    buttons = client.sent[-1][2]
    assert isinstance(buttons, list)
    assert [button.text for button in buttons[0]] == ["Move here", "Back", "Cancel"]


async def pending_sessions(queue: SQLiteJobQueue) -> list[object]:
    return await queue.list_pending_delivery_interactions()


async def seed_batch(
    queue: SQLiteJobQueue,
    storage: LocalStorage,
    client: FakeClient,
    *,
    batch_id: str,
    message_base: int,
    size: int = 2,
) -> list[object]:
    jobs = [
        await completed_file(
            queue,
            storage,
            message_id=message_base + index,
            batch_id=batch_id,
            batch_expected_size=size,
        )
        for index in range(size)
    ]
    notifier = TelegramJobNotifier(client, queue)
    for job in jobs:
        await notifier.job_finished(job)
    return jobs


async def batch_confirmation(
    bot: TelegramBotController,
    client: FakeClient,
    *,
    requester: int = 1001,
) -> FakeCallback:
    opened = FakeCallback(requester, button_data(move_all_buttons(client)[0]))
    await bot.handle_callback(opened)
    directories = opened.edit_buttons[0]
    assert isinstance(directories, list)
    select = FakeCallback(requester, button_data(directories[0][0]))
    await bot.handle_callback(select)
    confirmation = select.edit_buttons[0]
    assert isinstance(confirmation, list)
    return FakeCallback(requester, button_data(confirmation[0][0]))


async def test_single_result_final_edit_failure_retains_pending_and_recovers(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    session = await queue.create_interaction(
        1001,
        "archive",
        "confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
        pending_directory="videos",
        prompt_message_id=900,
    )
    # The working edit succeeds; the terminal edit is an ambiguous timeout that
    # must not trigger a replacement send.
    client.edit_error_sequence = [None, TimeoutError("ambiguous")]
    sent_before = len(client.sent)

    confirm = FakeCallback(1001, f"tg:{session.id}:confirm".encode(), message_id=900)
    await bot.handle_callback(confirm)

    assert "Working" in confirm.edits[0]
    # The ambiguous failure reports one generic notice, never a result duplicate.
    assert len(client.sent) == sent_before + 1
    assert "could not be delivered" in client.sent[-1][1]
    assert client.sent[-1][2] is None
    failed_after = len(client.sent)
    pending = await queue.get_interaction(session.id)
    assert pending is not None
    assert pending.step == "result-pending"
    assert pending.prompt_message_id == 900
    assert pending.pending_directory is None
    assert [item.id for item in await pending_sessions(queue)] == [session.id]
    stored = await queue.get_file(completed.id)
    assert stored is not None
    assert stored.relative_path == Path("archive/videos/clip.mp4")

    async def forbidden_organize(*args: object, **kwargs: object) -> object:
        raise AssertionError("recovery must not rerun organization")

    bot.library.organize = forbidden_organize  # type: ignore[method-assign]
    recovered = await bot.restore_pending_results(await pending_sessions(queue))
    assert recovered == 1
    assert await queue.get_interaction(session.id) is None
    assert len(client.sent) == failed_after
    assert "Completed" in client.edited[-1][2]
    assert client.edited[-1][1] == 900


async def test_single_result_delivery_client_cancellation_retains_pending(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    session = await queue.create_interaction(
        1001,
        "rename-and-archive",
        "confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
        pending_name="renamed.mp4",
        pending_directory="videos",
        prompt_message_id=905,
    )
    client.edit_error_sequence = [None, asyncio.CancelledError()]
    sent_before = len(client.sent)

    confirm = FakeCallback(1001, f"tg:{session.id}:confirm".encode(), message_id=905)
    with pytest.raises(asyncio.CancelledError):
        await bot.handle_callback(confirm)

    pending = await queue.get_interaction(session.id)
    assert pending is not None and pending.step == "result-pending"
    assert len(client.sent) == sent_before
    stored = await queue.get_file(completed.id)
    assert stored is not None
    assert stored.relative_path == Path("archive/videos/renamed.mp4")

    assert await bot.restore_pending_results(await pending_sessions(queue)) == 1
    assert await queue.get_interaction(session.id) is None
    assert "Completed" in client.edited[-1][2]


async def test_single_result_uneditable_final_edit_sends_one_bound_replacement(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    session = await queue.create_interaction(
        1001,
        "archive",
        "confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
        pending_directory="videos",
        prompt_message_id=907,
    )
    client.edit_error_sequence = [None, MessageIdInvalidError(None)]

    confirm = FakeCallback(1001, f"tg:{session.id}:confirm".encode(), message_id=907)
    await bot.handle_callback(confirm)

    assert await queue.get_interaction(session.id) is None
    assert len(client.sent) == 1
    _, text, buttons = client.sent[0]
    assert "Completed" in text
    assert buttons is None
    assert await pending_sessions(queue) == []


async def test_single_pending_stale_callback_and_unauthorized_recovery_are_safe(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    pending = await queue.create_interaction(
        1001,
        "archive",
        "result-pending",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
        prompt_message_id=910,
    )
    stale = FakeCallback(1001, f"tg:{pending.id}:confirm".encode(), message_id=911)
    await bot.handle_callback(stale)
    assert_acknowledged_first(stale.calls)
    assert "stale" in stale.responses[-1]
    retained = await queue.get_interaction(pending.id)
    assert retained is not None and retained.step == "result-pending"

    unauthorized = await queue.create_interaction(
        9999,
        "archive",
        "result-pending",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        prompt_message_id=912,
    )
    assert await bot.restore_pending_results(await pending_sessions(queue)) == 1
    assert await queue.get_interaction(pending.id) is None
    assert await queue.get_interaction(unauthorized.id) is not None
    assert all(message_id != 912 for _, message_id, _, _ in client.edited)
    assert await bot.restore_pending_results([unauthorized]) == 0
    assert await queue.get_interaction(unauthorized.id) is not None


async def test_single_pending_restart_ambiguous_failure_retains_then_recovers(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    session = await queue.create_interaction(
        1001,
        "archive",
        "result-pending",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
        prompt_message_id=915,
    )
    stored = await queue.get_file(completed.id)
    assert stored is not None
    client.edit_error = TimeoutError("ambiguous")
    sent_before = len(client.sent)

    assert await bot.restore_pending_results(await pending_sessions(queue)) == 0
    retained = await queue.get_interaction(session.id)
    assert retained is not None and retained.step == "result-pending"
    assert len(client.sent) == sent_before

    client.edit_error = None
    assert await bot.restore_pending_results(await pending_sessions(queue)) == 1
    assert await queue.get_interaction(session.id) is None


async def test_batch_partial_retry_failure_retains_pending_and_restart_recovers(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    jobs = await seed_batch(
        queue,
        storage,
        client,
        batch_id="batch-pending-retry",
        message_base=3000,
    )
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    blocked = await queue.get_file(jobs[1].id)
    assert blocked is not None
    blocked_path = storage.root / blocked.relative_path
    blocked_bytes = blocked_path.read_bytes()
    blocked_path.unlink()

    confirm = await batch_confirmation(bot, client)
    client.edit_error_sequence = [None, TimeoutError("ambiguous")]
    sent_before = len(client.sent)
    await bot.handle_callback(confirm)

    session_id = UUID(confirm.data.decode().split(":")[1])
    pending = await queue.get_interaction(session_id)
    # The retry card failed ambiguously: its non-actionable row is retained.
    assert pending is not None
    assert pending.step == "retry-pending"
    assert pending.pending_directory is None
    assert len(client.sent) == sent_before + 1
    assert "could not be delivered" in client.sent[-1][1]
    assert client.sent[-1][2] is None
    first = await queue.get_file(jobs[0].id)
    second = await queue.get_file(jobs[1].id)
    assert first is not None and first.relative_path.parts[0] == "archive"
    assert second is not None and second.relative_path.parts[0] == "inbox"

    original_batch = bot.library.organize_batch

    async def forbidden_batch(*args: object, **kwargs: object) -> object:
        raise AssertionError("recovery must not rerun batch organization")

    bot.library.organize_batch = forbidden_batch  # type: ignore[method-assign]
    try:
        recovered = await bot.restore_pending_results(await pending_sessions(queue))
    finally:
        bot.library.organize_batch = original_batch  # type: ignore[method-assign]
    assert recovered == 1
    ready = await queue.get_interaction(session_id)
    assert ready is not None and ready.step == "ready"
    retry_card = client.edited[-1]
    assert "Batch move result" in retry_card[2]
    assert "Choose a directory again to retry" in retry_card[2]
    buttons = retry_card[3]
    assert isinstance(buttons, list)
    assert [button.text for button in buttons[0]] == ["Retry move", "Cancel"]
    assert ready.prompt_message_id == retry_card[1]

    # Retry only moves the remaining Inbox member and never re-audits the moved one.
    blocked_path.write_bytes(blocked_bytes)
    retry = FakeCallback(1001, button_data(buttons[0][0]), message_id=retry_card[1])
    await bot.handle_callback(retry)
    directories = retry.edit_buttons[0]
    assert isinstance(directories, list)
    select = FakeCallback(1001, button_data(directories[0][0]), message_id=retry_card[1])
    await bot.handle_callback(select)
    confirmation = select.edit_buttons[0]
    assert isinstance(confirmation, list)
    final = FakeCallback(1001, button_data(confirmation[0][0]), message_id=retry_card[1])
    await bot.handle_callback(final)

    assert "Moved: 1" in final.last_text
    assert "Already moved: 1" in final.last_text
    assert await queue.get_interaction(session_id) is None
    for job in jobs:
        item = await queue.get_file(job.id)
        assert item is not None
        assert item.relative_path.parent == Path("archive/videos")
    async with aiosqlite.connect(queue.path) as connection:
        rows = await (
            await connection.execute("SELECT job_id, COUNT(*) FROM file_audit GROUP BY job_id")
        ).fetchall()
    counts = {row[0]: row[1] for row in rows if row[0] is not None}
    assert counts == {str(job.id): 1 for job in jobs}


async def test_batch_retry_delivery_cancellation_retains_pending(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    jobs = await seed_batch(
        queue,
        storage,
        client,
        batch_id="batch-pending-cancel",
        message_base=3020,
    )
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    blocked = await queue.get_file(jobs[1].id)
    assert blocked is not None
    blocked_path = storage.root / blocked.relative_path
    blocked_path.unlink()

    confirm = await batch_confirmation(bot, client)
    client.edit_error_sequence = [None, asyncio.CancelledError()]
    sent_before = len(client.sent)
    with pytest.raises(asyncio.CancelledError):
        await bot.handle_callback(confirm)

    sessions = await pending_sessions(queue)
    assert len(sessions) == 1
    session_id = sessions[0].id
    assert sessions[0].step == "retry-pending"
    assert len(client.sent) == sent_before

    # A callback on the non-actionable pending row is stale and mutates nothing.
    stale = FakeCallback(1001, f"tg:{session_id}:batch-move".encode())
    await bot.handle_callback(stale)
    assert_acknowledged_first(stale.calls)
    assert "stale" in stale.responses[-1]
    retained = await queue.get_interaction(session_id)
    assert retained is not None and retained.step == "retry-pending"

    assert await bot.restore_pending_results(await pending_sessions(queue)) == 1
    ready = await queue.get_interaction(session_id)
    assert ready is not None and ready.step == "ready"


async def test_batch_terminal_delivery_failure_retains_pending_and_recovers(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    jobs = await seed_batch(
        queue,
        storage,
        client,
        batch_id="batch-pending-terminal",
        message_base=3040,
    )
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")

    confirm = await batch_confirmation(bot, client)
    client.edit_error_sequence = [None, TimeoutError("ambiguous")]
    sent_before = len(client.sent)
    await bot.handle_callback(confirm)

    sessions = await pending_sessions(queue)
    assert len(sessions) == 1
    session_id = sessions[0].id
    assert sessions[0].step == "result-pending"
    assert len(client.sent) == sent_before + 1
    assert "could not be delivered" in client.sent[-1][1]
    assert client.sent[-1][2] is None
    for job in jobs:
        item = await queue.get_file(job.id)
        assert item is not None
        assert item.relative_path.parent == Path("archive/videos")

    async def forbidden_batch(*args: object, **kwargs: object) -> object:
        raise AssertionError("recovery must not rerun batch organization")

    bot.library.organize_batch = forbidden_batch  # type: ignore[method-assign]
    assert await bot.restore_pending_results(await pending_sessions(queue)) == 1
    assert await queue.get_interaction(session_id) is None
    assert "Batch move result" in client.edited[-1][2]


async def test_batch_terminal_delivery_outer_cancellation_settles_result(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    jobs = await seed_batch(
        queue,
        storage,
        client,
        batch_id="batch-pending-outer-cancel",
        message_base=3060,
    )
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")

    confirm = await batch_confirmation(bot, client)
    session_id = UUID(confirm.data.decode().split(":")[1])
    client.edit_gate_text = "Batch move result:"
    task = asyncio.create_task(bot.handle_callback(confirm))
    await asyncio.wait_for(client.edit_gate_entered.wait(), timeout=2)
    task.cancel()
    await asyncio.sleep(0)
    client.edit_gate_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await queue.get_interaction(session_id) is None
    assert await pending_sessions(queue) == []
    for job in jobs:
        item = await queue.get_file(job.id)
        assert item is not None
        assert item.relative_path.parent == Path("archive/videos")


async def test_batch_expired_pending_is_recoverable_and_unauthorized_is_retained(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    await seed_batch(
        queue,
        storage,
        client,
        batch_id="batch-expired-pending",
        message_base=3080,
    )
    expired = await queue.create_interaction(
        1001,
        "batch-move",
        "result-pending",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
        batch_id="batch-expired-pending",
        prompt_message_id=930,
    )
    unauthorized = await queue.create_interaction(
        9999,
        "batch-move",
        "retry-pending",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
        batch_id="batch-expired-pending",
        prompt_message_id=931,
    )

    listed = await pending_sessions(queue)
    assert {item.id for item in listed} == {expired.id, unauthorized.id}
    assert await bot.restore_pending_results(listed) == 1
    assert await queue.get_interaction(expired.id) is None
    retained = await queue.get_interaction(unauthorized.id)
    assert retained is not None and retained.step == "retry-pending"
    assert "Batch move result" in client.edited[-1][2]


async def test_single_finish_organization_cancellation_after_commit_settles_pending(
    tmp_path: Path,
) -> None:
    bot, queue, storage, _client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    session = await queue.create_interaction(
        1001,
        "archive",
        "confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
        pending_directory="videos",
        prompt_message_id=904,
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    original = bot.library.storage.finish_organization

    async def gated_finish(job_id: object) -> None:
        entered.set()
        await release.wait()
        return await original(job_id)  # type: ignore[arg-type]

    bot.library.storage.finish_organization = gated_finish  # type: ignore[method-assign]
    event = FakeCallback(1001, f"tg:{session.id}:confirm".encode(), message_id=904)
    task = asyncio.create_task(bot.handle_callback(event))
    await asyncio.wait_for(entered.wait(), timeout=2)

    # The catalog move already committed before finish_organization was gated.
    # Cancelling here must never restore the confirmation.
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    pending = await queue.get_interaction(session.id)
    assert pending is not None and pending.step == "result-pending"
    assert pending.prompt_message_id == 904
    stored = await queue.get_file(completed.id)
    assert stored is not None
    assert stored.relative_path == Path("archive/videos/clip.mp4")

    # Recovery delivers the already-committed result, and replaying the same
    # move adds no duplicate audit row.
    assert await bot.restore_pending_results(await pending_sessions(queue)) == 1
    assert await queue.get_interaction(session.id) is None
    await queue.record_file_move(
        completed.id,
        1001,
        stored.relative_path,
        storage.root / stored.relative_path,
        action="archive",
    )
    replay = await recover_pending_organizations(queue, storage)
    assert (replay.discovered, replay.recovered, replay.failed) == (0, 0, 0)
    async with aiosqlite.connect(queue.path) as connection:
        count = await (
            await connection.execute(
                "SELECT COUNT(*) FROM file_audit WHERE job_id = ?",
                (str(completed.id),),
            )
        ).fetchone()
    assert count == (1,)


async def test_expired_retry_pending_recovery_makes_retry_clickable(tmp_path: Path) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    jobs = await seed_batch(
        queue,
        storage,
        client,
        batch_id="batch-expired-retry-approved",
        message_base=3100,
    )
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    session = await queue.create_interaction(
        1001,
        "batch-move",
        "retry-pending",
        expires_at=datetime.now(UTC) - timedelta(hours=2),
        batch_id="batch-expired-retry-approved",
        prompt_message_id=940,
    )

    assert await bot.restore_pending_results(await pending_sessions(queue)) == 1

    ready = await queue.get_interaction(session.id)
    assert ready is not None and ready.step == "ready"
    assert ready.prompt_message_id == 940
    assert ready.expires_at > datetime.now(UTC)
    retry_card = client.edited[-1]
    assert retry_card[1] == 940
    buttons = retry_card[3]
    assert isinstance(buttons, list)
    assert [button.text for button in buttons[0]] == ["Retry move", "Cancel"]

    # The renewed expiry makes the Retry callback actionably accepted.
    retry = FakeCallback(1001, button_data(buttons[0][0]), message_id=940)
    await bot.handle_callback(retry)
    active = await queue.get_interaction(session.id)
    assert active is not None and active.step == "await-directory"
    directories = retry.edit_buttons[0]
    assert isinstance(directories, list)
    select = FakeCallback(1001, button_data(directories[0][0]), message_id=940)
    await bot.handle_callback(select)
    confirmation = select.edit_buttons[0]
    assert isinstance(confirmation, list)
    final = FakeCallback(1001, button_data(confirmation[0][0]), message_id=940)
    await bot.handle_callback(final)

    assert await queue.get_interaction(session.id) is None
    for job in jobs:
        item = await queue.get_file(job.id)
        assert item is not None
        assert item.relative_path.parent == Path("archive/videos")


async def test_batch_mid_member_cancellation_settles_retry_pending(tmp_path: Path) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    jobs = await seed_batch(
        queue,
        storage,
        client,
        batch_id="batch-mid-cancel",
        message_base=3120,
    )
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    blocked = await queue.get_file(jobs[1].id)
    assert blocked is not None
    blocked_path = storage.root / blocked.relative_path
    blocked_bytes = blocked_path.read_bytes()
    blocked_path.unlink()

    entered = asyncio.Event()
    release = asyncio.Event()
    original = bot.library.organize
    calls = 0

    async def gated_organize(*args: object, **kwargs: object) -> object:
        nonlocal calls
        result = await original(*args, **kwargs)
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return result

    bot.library.organize = gated_organize  # type: ignore[method-assign]
    confirm = await batch_confirmation(bot, client)
    session_id = UUID(confirm.data.decode().split(":")[1])
    task = asyncio.create_task(bot.handle_callback(confirm))
    await asyncio.wait_for(entered.wait(), timeout=2)

    # One member has committed. The shielded batch finishes the remaining
    # Inbox members and settles a retry-pending result before cancellation
    # propagates; it never restores the pristine confirmation.
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    pending = await queue.get_interaction(session_id)
    assert pending is not None and pending.step == "retry-pending"
    first = await queue.get_file(jobs[0].id)
    second = await queue.get_file(jobs[1].id)
    assert first is not None and first.relative_path.parts[0] == "archive"
    assert second is not None and second.relative_path.parts[0] == "inbox"

    blocked_path.write_bytes(blocked_bytes)
    assert await bot.restore_pending_results(await pending_sessions(queue)) == 1
    ready = await queue.get_interaction(session_id)
    assert ready is not None and ready.step == "ready"

    retry_card = client.edited[-1]
    buttons = retry_card[3]
    assert isinstance(buttons, list)
    retry = FakeCallback(1001, button_data(buttons[0][0]), message_id=retry_card[1])
    await bot.handle_callback(retry)
    directories = retry.edit_buttons[0]
    assert isinstance(directories, list)
    select = FakeCallback(1001, button_data(directories[0][0]), message_id=retry_card[1])
    await bot.handle_callback(select)
    confirmation = select.edit_buttons[0]
    assert isinstance(confirmation, list)
    final = FakeCallback(1001, button_data(confirmation[0][0]), message_id=retry_card[1])
    await bot.handle_callback(final)

    assert "Moved: 1" in final.last_text
    assert "Already moved: 1" in final.last_text
    assert await queue.get_interaction(session_id) is None
    for job in jobs:
        item = await queue.get_file(job.id)
        assert item is not None
        assert item.relative_path.parent == Path("archive/videos")
    async with aiosqlite.connect(queue.path) as connection:
        rows = await (
            await connection.execute("SELECT job_id, COUNT(*) FROM file_audit GROUP BY job_id")
        ).fetchall()
    counts = {row[0]: row[1] for row in rows if row[0] is not None}
    assert counts == {str(job.id): 1 for job in jobs}


async def test_single_fallback_send_without_message_id_retains_pending(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    session = await queue.create_interaction(
        1001,
        "archive",
        "confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=completed.id,
        pending_directory="videos",
        prompt_message_id=945,
    )

    async def missing_id_send(actor_id: int, text: str, *, buttons: object | None = None) -> object:
        return SimpleNamespace(id=0)

    client.send_message = missing_id_send  # type: ignore[method-assign]
    client.edit_error_sequence = [None, MessageIdInvalidError(None)]

    confirm = FakeCallback(1001, f"tg:{session.id}:confirm".encode(), message_id=945)
    await bot.handle_callback(confirm)

    pending = await queue.get_interaction(session.id)
    assert pending is not None and pending.step == "result-pending"
    stored = await queue.get_file(completed.id)
    assert stored is not None and stored.relative_path.parts[0] == "archive"


async def test_batch_retry_fallback_send_without_message_id_keeps_retry_pending(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    jobs = await seed_batch(
        queue,
        storage,
        client,
        batch_id="batch-missing-id",
        message_base=3140,
    )
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    blocked = await queue.get_file(jobs[1].id)
    assert blocked is not None
    (storage.root / blocked.relative_path).unlink()

    confirm = await batch_confirmation(bot, client)
    session_id = UUID(confirm.data.decode().split(":")[1])

    async def missing_id_send(actor_id: int, text: str, *, buttons: object | None = None) -> object:
        return SimpleNamespace(id=-1)

    client.send_message = missing_id_send  # type: ignore[method-assign]
    client.edit_error_sequence = [None, MessageIdInvalidError(None)]

    await bot.handle_callback(confirm)

    pending = await queue.get_interaction(session_id)
    assert pending is not None and pending.step == "retry-pending"
    assert pending.prompt_message_id is not None


async def test_expired_working_confirm_is_recovered_renewed_and_clickable(
    tmp_path: Path,
) -> None:
    bot, queue, storage, client = await full_controller(tmp_path)
    completed = await completed_file(queue, storage)
    assert bot.library is not None
    await bot.library.mkdir(1001, "videos")
    session = await queue.create_interaction(
        1001,
        "archive",
        "working-confirm",
        expires_at=datetime.now(UTC) - timedelta(hours=2),
        job_id=completed.id,
        pending_name="clip.mp4",
        pending_directory="videos",
        prompt_message_id=950,
    )
    renewed_expiry = datetime.now(UTC) + timedelta(hours=1)

    recovered = await queue.recover_working_interactions(expires_at=renewed_expiry)
    assert [item.id for item in recovered] == [session.id]
    assert recovered[0].expires_at == renewed_expiry
    assert await bot.restore_interaction_cards(recovered) == 1

    _, message_id, text, buttons = client.edited[-1]
    assert message_id == 950
    assert "Confirm file organization:" in text
    assert isinstance(buttons, list)
    click = FakeCallback(1001, button_data(buttons[0][0]), message_id=950)
    await bot.handle_callback(click)

    stored = await queue.get_file(completed.id)
    assert stored is not None
    assert stored.relative_path == Path("archive/videos/clip.mp4")
    assert "Completed" in click.edits[-1]

    # Replaying the same move and organization reconciliation adds no audit row.
    await queue.record_file_move(
        completed.id,
        1001,
        stored.relative_path,
        storage.root / stored.relative_path,
        action="archive",
    )
    replay = await recover_pending_organizations(queue, storage)
    assert (replay.discovered, replay.recovered, replay.failed) == (0, 0, 0)
    async with aiosqlite.connect(queue.path) as connection:
        count = await (
            await connection.execute(
                "SELECT COUNT(*) FROM file_audit WHERE job_id = ?",
                (str(completed.id),),
            )
        ).fetchone()
    assert count == (1,)
