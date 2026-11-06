from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from tgharbor.core.job_api import JobAPI
from tgharbor.core.jobs import SourceSession
from tgharbor.persistence import SQLiteJobQueue
from tgharbor.telegram.bot import TelegramBotController


class FakeClient:
    def add_event_handler(self, callback: object, event: object) -> None:
        pass

    def remove_event_handler(self, callback: object, event: object) -> None:
        pass


class FakeEvent:
    def __init__(
        self,
        *,
        sender_id: int,
        text: str = "",
        media: object | None = None,
        chat_id: int = 1001,
        message_id: int = 10,
    ) -> None:
        self.sender_id = sender_id
        self.raw_text = text
        self.message = SimpleNamespace(media=media)
        self.chat_id = chat_id
        self.id = message_id
        self.responses: list[str] = []

    async def respond(self, text: str) -> None:
        self.responses.append(text)


async def controller(tmp_path: Path) -> tuple[TelegramBotController, SQLiteJobQueue]:
    queue = SQLiteJobQueue(tmp_path / "jobs.db")
    await queue.initialize()
    api = JobAPI(queue, (1001,), max_attempts=3)
    return TelegramBotController(FakeClient(), api), queue


async def test_unauthorized_user_cannot_submit(tmp_path: Path) -> None:
    bot, queue = await controller(tmp_path)
    event = FakeEvent(sender_id=9999, media=object())

    await bot.handle_event(event)

    assert event.responses == ["Unauthorized."]
    assert await queue.claim_next("worker") is None


async def test_media_is_enqueued_for_bot_session(tmp_path: Path) -> None:
    bot, queue = await controller(tmp_path)
    event = FakeEvent(sender_id=1001, media=object(), chat_id=1001, message_id=77)

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


async def test_status_and_cancel_are_owner_scoped(tmp_path: Path) -> None:
    bot, queue = await controller(tmp_path)
    submit = FakeEvent(sender_id=1001, media=object(), message_id=99)
    await bot.handle_event(submit)
    job = (await queue.latest_for_requester(1001))[0]

    status = FakeEvent(sender_id=1001, text=f"/status {job.id}")
    await bot.handle_event(status)
    cancel = FakeEvent(sender_id=1001, text=f"/cancel {job.id}")
    await bot.handle_event(cancel)

    assert "pending" in status.responses[0]
    assert "cancelled" in cancel.responses[0]
