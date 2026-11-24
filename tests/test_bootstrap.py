from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import aiosqlite
import pytest
from telethon.errors import MessageIdInvalidError

import tgharbor
from tgharbor.app.bootstrap import run
from tgharbor.app.instance_lock import InstanceAlreadyRunningError, InstanceLock
from tgharbor.config import AppConfig, load_config
from tgharbor.core.jobs import NewDownloadJob, SourceSession
from tgharbor.persistence import SQLiteJobQueue
from tgharbor.telegram.sessions import SessionAuthorizationError, TelegramClients


class FakeBotClient:
    def __init__(self) -> None:
        self.started = False
        self.disconnected = False
        self.handlers: list[tuple[object, object]] = []
        self.sent: list[tuple[int, str, object | None]] = []
        self.sent_ids: list[int] = []
        self.edited: list[tuple[int, int, str, object | None]] = []
        self.fail_ids: set[int] = set()
        self.edit_fail_ids: set[int] = set()
        self.edit_error: BaseException | None = None
        self.events: list[tuple[str, str]] = []
        self.command_failures: set[str] = set()
        self._next_message_id = 9000

    async def start(self, *, bot_token: str) -> None:
        self.started = True
        self.events.append(("start", ""))

    async def __call__(self, request: object) -> None:
        scope_name = type(request.scope).__name__  # type: ignore[attr-defined]
        self.events.append(("command", scope_name))
        if scope_name in self.command_failures:
            raise RuntimeError("private registration transport detail")

    async def disconnect(self) -> None:
        self.disconnected = True

    async def run_until_disconnected(self) -> None:
        return None

    def add_event_handler(self, callback: object, event: object) -> None:
        self.handlers.append((callback, event))

    def remove_event_handler(self, callback: object, event: object) -> None:
        self.handlers.remove((callback, event))

    async def send_message(
        self,
        requester_id: int,
        text: str,
        *,
        buttons: object | None = None,
    ) -> SimpleNamespace:
        if requester_id in self.fail_ids:
            raise RuntimeError("private transport detail")
        self._next_message_id += 1
        self.sent.append((requester_id, text, buttons))
        self.sent_ids.append(self._next_message_id)
        self.events.append(("send", text))
        return SimpleNamespace(id=self._next_message_id)

    async def edit_message(
        self,
        actor_id: int,
        message_id: int,
        text: str,
        *,
        buttons: object | None = None,
    ) -> SimpleNamespace:
        if actor_id in self.edit_fail_ids:
            raise self.edit_error or RuntimeError("private edit detail")
        self.edited.append((actor_id, message_id, text, buttons))
        self.events.append(("edit", text))
        return SimpleNamespace(id=message_id)


class FakeUserClient:
    def __init__(self, *, authorized: bool = True) -> None:
        self.authorized = authorized
        self.connected = False
        self.disconnected = False

    async def connect(self) -> None:
        self.connected = True

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def disconnect(self) -> None:
        self.disconnected = True


def make_config(
    tmp_path: Path,
    *,
    authorized_users: tuple[int, ...] = (1001,),
) -> AppConfig:
    source = tmp_path / "config.yaml"
    users = ", ".join(str(user) for user in authorized_users)
    source.write_text(
        f"""
telegram:
  api_id: 12345
  api_hash: test-hash
  bot_token: test-token
  authorized_users: [{users}]
downloads:
  path: downloads
  concurrency: 1
  timeout_seconds: 1
  max_attempts: 2
  retry_base_seconds: 1
database:
  path: state/tgharbor.db
sessions:
  path: state/sessions
logging:
  level: INFO
""",
        encoding="utf-8",
    )
    return load_config(source, environ={})


async def test_bootstrap_assembles_and_cleans_up(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    bot = FakeBotClient()
    user = FakeUserClient()

    await run(config, clients=TelegramClients(bot=bot, user=user))

    assert bot.started and user.connected
    assert bot.disconnected and user.disconnected
    assert bot.handlers == []
    assert config.database.path.exists()
    assert config.downloads.path.is_dir()


async def test_bootstrap_cleans_up_after_unauthorized_user_session(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    bot = FakeBotClient()
    user = FakeUserClient(authorized=False)

    with pytest.raises(SessionAuthorizationError, match="tgharbor login"):
        await run(config, clients=TelegramClients(bot=bot, user=user))

    assert bot.disconnected and user.disconnected
    lock = InstanceLock(config.database.path)
    lock.acquire()
    lock.release()


def test_instance_lock_rejects_second_owner(tmp_path: Path) -> None:
    database = tmp_path / "state/tgharbor.db"
    first = InstanceLock(database)
    second = InstanceLock(database)
    first.acquire()
    try:
        with pytest.raises(InstanceAlreadyRunningError):
            second.acquire()
    finally:
        first.release()


async def seed_running_jobs(config: AppConfig, users: tuple[int, ...]) -> None:
    queue = SQLiteJobQueue(config.database.path)
    await queue.initialize()
    for requester_id in users:
        await queue.enqueue(
            NewDownloadJob(
                requester_id=requester_id,
                source_session=SourceSession.BOT,
                source_chat=str(requester_id),
                source_message_id=1000 + requester_id,
            )
        )
        running = await queue.claim_next(f"worker-{requester_id}")
        assert running is not None


async def test_bootstrap_sends_startup_notice_to_each_authorized_user(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path, authorized_users=(1001, 2002))
    bot = FakeBotClient()

    await run(config, clients=TelegramClients(bot=bot, user=FakeUserClient()))

    assert [requester for requester, _, _ in bot.sent] == [1001, 2002]
    for _, text, buttons in bot.sent:
        assert f"TGHarbor {tgharbor.__version__}" in text
        assert "Server time: " in text
        assert "Interrupted tasks: 0" in text
        assert "No interrupted tasks" in text
        assert buttons is None


async def test_bootstrap_notices_are_requester_scoped_and_paused_jobs_survive_restart(
    tmp_path: Path,
) -> None:
    users = (1001, 2002)
    config = make_config(tmp_path, authorized_users=users)
    await seed_running_jobs(config, users)

    first = FakeBotClient()
    await run(config, clients=TelegramClients(bot=first, user=FakeUserClient()))
    first_by_user = {requester: (text, buttons) for requester, text, buttons in first.sent}
    assert set(first_by_user) == set(users)
    for text, buttons in first_by_user.values():
        assert "Interrupted tasks: 1" in text
        assert isinstance(buttons, list)
        assert [button.text for button in buttons[0]] == [
            "View interrupted tasks",
            "Continue interrupted tasks",
        ]

    second = FakeBotClient()
    await run(config, clients=TelegramClients(bot=second, user=FakeUserClient()))
    second_by_user = {requester: text for requester, text, _ in second.sent}
    assert set(second_by_user) == set(users)
    assert all("Interrupted tasks: 1" in text for text in second_by_user.values())

    queue = SQLiteJobQueue(config.database.path)
    await queue.initialize()
    for requester_id in users:
        paused = await queue.list_paused_for_requester(requester_id)
        assert len(paused) == 1
        assert paused[0].resume_required


async def test_bootstrap_restores_working_interaction_after_restart(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    queue = SQLiteJobQueue(config.database.path)
    await queue.initialize()
    session = await queue.create_interaction(
        1001,
        "archive",
        "working-confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        prompt_message_id=321,
        pending_name="clip.mp4",
        pending_directory="videos",
    )
    bot = FakeBotClient()

    await run(config, clients=TelegramClients(bot=bot, user=FakeUserClient()))

    restored = await queue.get_interaction(session.id)
    assert restored is not None
    assert restored.step == "confirm"
    assert restored.prompt_message_id == 321
    assert restored.pending_name == "clip.mp4"
    assert restored.pending_directory == "videos"
    assert len(bot.edited) == 1
    actor_id, message_id, text, buttons = bot.edited[0]
    assert actor_id == 1001
    assert message_id == 321
    assert "Confirm file organization:" in text
    assert "Name: clip.mp4" in text
    assert "Directory: videos" in text
    assert isinstance(buttons, list)
    assert [button.text for button in buttons[0]] == ["Move here", "Back", "Cancel"]


async def test_bootstrap_restores_new_directory_and_legacy_retry_controls(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    queue = SQLiteJobQueue(config.database.path)
    await queue.initialize()
    new_directory = await queue.create_interaction(
        1001,
        "rename-and-archive",
        "working-confirm-new-directory",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        prompt_message_id=411,
        pending_name="renamed.mp4",
        pending_directory="videos/new",
    )
    legacy = await queue.create_interaction(
        1001,
        "batch-move",
        "working",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        prompt_message_id=412,
        pending_directory="videos",
    )
    bot = FakeBotClient()

    await run(config, clients=TelegramClients(bot=bot, user=FakeUserClient()))

    restored_new = await queue.get_interaction(new_directory.id)
    assert restored_new is not None and restored_new.step == "confirm-new-directory"
    restored_legacy = await queue.get_interaction(legacy.id)
    assert restored_legacy is not None and restored_legacy.step == "ready"
    assert restored_legacy.pending_directory is None
    cards = {message_id: (text, buttons) for _, message_id, text, buttons in bot.edited}
    new_text, new_buttons = cards[411]
    assert "Confirm file organization:" in new_text
    assert "Name: renamed.mp4" in new_text
    assert "Directory: videos/new (will be created)" in new_text
    assert isinstance(new_buttons, list)
    assert [button.text for button in new_buttons[0]] == ["Create and move", "Back", "Cancel"]
    retry_text, retry_buttons = cards[412]
    assert "The previous action was interrupted." in retry_text
    assert isinstance(retry_buttons, list)
    assert [button.text for button in retry_buttons[0]] == ["Retry move", "Cancel"]


async def test_bootstrap_recovery_fallback_send_binds_cas_anchor(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    queue = SQLiteJobQueue(config.database.path)
    await queue.initialize()
    session = await queue.create_interaction(
        1001,
        "archive",
        "working-confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        prompt_message_id=555,
        pending_directory="videos",
    )
    bot = FakeBotClient()
    bot.edit_fail_ids = {1001}
    bot.edit_error = MessageIdInvalidError(None)

    await run(config, clients=TelegramClients(bot=bot, user=FakeUserClient()))

    assert bot.edited == []
    card_indexes = [
        index for index, (_, text, _) in enumerate(bot.sent) if "Confirm file organization:" in text
    ]
    assert len(card_indexes) == 1
    replacement_id = bot.sent_ids[card_indexes[0]]
    restored = await queue.get_interaction(session.id)
    assert restored is not None
    assert restored.step == "confirm"
    assert restored.prompt_message_id == replacement_id
    assert restored.pending_directory == "videos"


async def test_bootstrap_recovery_isolates_actors_and_failures(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = make_config(tmp_path, authorized_users=(1001, 2002))
    queue = SQLiteJobQueue(config.database.path)
    await queue.initialize()
    healthy = await queue.create_interaction(
        1001,
        "archive",
        "working-confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        prompt_message_id=611,
        pending_directory="videos",
    )
    broken = await queue.create_interaction(
        2002,
        "archive",
        "working-confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        prompt_message_id=622,
        pending_directory="videos",
    )
    await seed_running_jobs(config, (2002,))
    bot = FakeBotClient()
    bot.edit_fail_ids = {2002}
    bot.edit_error = MessageIdInvalidError(None)
    bot.fail_ids = {2002}
    caplog.set_level(logging.WARNING, logger="tgharbor.telegram.bot")

    await run(config, clients=TelegramClients(bot=bot, user=FakeUserClient()))

    assert [(actor_id, message_id) for actor_id, message_id, _, _ in bot.edited] == [(1001, 611)]
    assert await queue.get_interaction(healthy.id) is not None
    paused = await queue.list_paused_for_requester(2002)
    assert len(paused) == 1 and paused[0].resume_required
    broken_restored = await queue.get_interaction(broken.id)
    assert broken_restored is not None
    assert broken_restored.step == "confirm"
    assert broken_restored.prompt_message_id == 622
    assert "outcome=not-visible step=confirm category=send" in caplog.text
    assert "611" not in caplog.text
    assert "622" not in caplog.text
    assert "1001" not in caplog.text
    assert "2002" not in caplog.text


async def test_bootstrap_recovery_skips_unauthorized_actor(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    queue = SQLiteJobQueue(config.database.path)
    await queue.initialize()
    session = await queue.create_interaction(
        2002,
        "archive",
        "working-confirm",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        prompt_message_id=630,
        pending_directory="videos",
    )
    bot = FakeBotClient()

    await run(config, clients=TelegramClients(bot=bot, user=FakeUserClient()))

    restored = await queue.get_interaction(session.id)
    assert restored is not None and restored.step == "confirm"
    assert restored.prompt_message_id == 630
    assert bot.edited == []
    assert {actor_id for actor_id, _, _ in bot.sent} == {1001}


async def test_bootstrap_restart_makes_buttonless_working_card_actionable(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    queue = SQLiteJobQueue(config.database.path)
    await queue.initialize()
    session = await queue.create_interaction(
        1001,
        "archive",
        "working",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        prompt_message_id=700,
    )
    bot = FakeBotClient()

    await run(config, clients=TelegramClients(bot=bot, user=FakeUserClient()))

    restored = await queue.get_interaction(session.id)
    assert restored is not None and restored.step == "ready"
    assert len(bot.edited) == 1
    _, message_id, text, buttons = bot.edited[0]
    assert message_id == 700
    assert "interrupted" in text
    assert isinstance(buttons, list)
    assert [button.text for button in buttons[0]] == ["Choose folder", "Cancel"]


async def test_bootstrap_redrives_pending_batch_notice_at_startup(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    queue = SQLiteJobQueue(config.database.path)
    await queue.initialize()
    for index in range(2):
        await queue.enqueue(
            NewDownloadJob(
                requester_id=1001,
                source_session=SourceSession.BOT,
                source_chat="1001",
                source_message_id=2000 + index,
                batch_id="batch-startup-redrive",
                batch_expected_size=2,
            )
        )
        running = await queue.claim_next(f"seed-{index}")
        assert running is not None
        await queue.mark_completed(
            running.id,
            f"seed-{index}",
            Path(f"/downloads/{index}.bin"),
            relative_path=Path(f"inbox/{index}.bin"),
        )
    assert await queue.claim_batch_notification(
        "batch-startup-redrive", 1001, claimed_by="prepared"
    )
    assert await queue.release_batch_notification(
        "batch-startup-redrive", 1001, claimed_by="prepared"
    )

    bot = FakeBotClient()
    await run(config, clients=TelegramClients(bot=bot, user=FakeUserClient()))

    messages = [text for _, text, _ in bot.sent]
    assert sum("Album download completed" in text for text in messages) == 1
    assert any(f"TGHarbor {tgharbor.__version__}" in text for text in messages)
    async with aiosqlite.connect(config.database.path) as connection:
        cursor = await connection.execute(
            "SELECT state FROM batch_notifications WHERE requester_id = ? AND batch_id = ?",
            (1001, "batch-startup-redrive"),
        )
        row = await cursor.fetchone()
    assert row is not None and row[0] == "sent"


async def test_bootstrap_registers_command_menu_before_startup_notices(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    bot = FakeBotClient()
    user = FakeUserClient()

    await run(config, clients=TelegramClients(bot=bot, user=user))

    scopes = [payload for kind, payload in bot.events if kind == "command"]
    assert scopes == ["BotCommandScopeDefault", "BotCommandScopeUsers"]
    first_command = next(index for index, event in enumerate(bot.events) if event[0] == "command")
    first_send = next(index for index, event in enumerate(bot.events) if event[0] == "send")
    assert first_command < first_send


async def test_bootstrap_command_registration_failure_is_isolated(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = make_config(tmp_path)
    bot = FakeBotClient()
    bot.command_failures = {"BotCommandScopeDefault"}
    user = FakeUserClient()
    caplog.set_level(logging.WARNING, logger="tgharbor.telegram.bot")

    await run(config, clients=TelegramClients(bot=bot, user=user))

    assert bot.started and bot.disconnected
    assert [payload for kind, payload in bot.events if kind == "command"] == [
        "BotCommandScopeDefault",
        "BotCommandScopeUsers",
    ]
    assert any(kind == "send" for kind, _ in bot.events)
    assert "outcome=failed scope=default error_type=RuntimeError" in caplog.text
    assert "private registration transport detail" not in caplog.text
    assert "1001" not in caplog.text


async def test_bootstrap_startup_send_failure_is_isolated(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = make_config(tmp_path, authorized_users=(1001, 2002))
    bot = FakeBotClient()
    bot.fail_ids.add(1001)
    caplog.set_level(logging.WARNING, logger="tgharbor.app.bootstrap")

    await run(config, clients=TelegramClients(bot=bot, user=FakeUserClient()))

    assert [requester for requester, _, _ in bot.sent] == [2002]
    assert "Startup notification failed" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    assert "private transport detail" not in caplog.text


async def seed_completed(
    config: AppConfig,
    *,
    requester_id: int = 1001,
    message_id: int = 4000,
    batch_id: str | None = None,
    batch_expected_size: int = 1,
) -> tuple[SQLiteJobQueue, UUID]:
    queue = SQLiteJobQueue(config.database.path)
    await queue.initialize()
    job = await queue.enqueue(
        NewDownloadJob(
            requester_id=requester_id,
            source_session=SourceSession.BOT,
            source_chat=str(requester_id),
            source_message_id=message_id,
            batch_id=batch_id,
            batch_expected_size=batch_expected_size,
        )
    )
    running = await queue.claim_next(f"seed-{message_id}")
    assert running is not None and running.id == job.id
    await queue.mark_completed(
        running.id,
        f"seed-{message_id}",
        Path(f"/downloads/inbox/{message_id}.bin"),
        relative_path=Path(f"inbox/{message_id}.bin"),
    )
    return queue, job.id


async def test_bootstrap_restores_pending_result_after_reconciliation_before_callbacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(tmp_path)
    queue, job_id = await seed_completed(config, message_id=5000)
    session = await queue.create_interaction(
        1001,
        "archive",
        "result-pending",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=job_id,  # type: ignore[arg-type]
        prompt_message_id=500,
    )
    bot = FakeBotClient()
    import tgharbor.app.bootstrap as bootstrap_module

    original_reconcile = bootstrap_module.recover_pending_organizations

    async def record_reconciliation(repository: object, storage: object) -> object:
        bot.events.append(("reconcile", ""))
        return await original_reconcile(repository, storage)  # type: ignore[arg-type]

    monkeypatch.setattr(bootstrap_module, "recover_pending_organizations", record_reconciliation)
    original_add_handler = bot.add_event_handler

    def record_handler(callback: object, event: object) -> None:
        bot.events.append(("handler", ""))
        original_add_handler(callback, event)

    bot.add_event_handler = record_handler  # type: ignore[method-assign]

    await run(config, clients=TelegramClients(bot=bot, user=FakeUserClient()))

    assert await queue.get_interaction(session.id) is None
    kinds = [kind for kind, _ in bot.events]
    assert kinds.index("reconcile") < kinds.index("edit")
    assert kinds.index("edit") < kinds.index("handler")
    _, message_id, text, buttons = bot.edited[0]
    assert message_id == 500
    assert "Completed" in text
    assert buttons is None


async def test_bootstrap_restores_retry_pending_then_marks_ready(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    for index in range(2):
        await seed_completed(
            config,
            message_id=5100 + index,
            batch_id="batch-bootstrap-retry",
            batch_expected_size=2,
        )
    queue = SQLiteJobQueue(config.database.path)
    await queue.initialize()
    session = await queue.create_interaction(
        1001,
        "batch-move",
        "retry-pending",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
        batch_id="batch-bootstrap-retry",
        prompt_message_id=600,
    )
    bot = FakeBotClient()

    await run(config, clients=TelegramClients(bot=bot, user=FakeUserClient()))

    ready = await queue.get_interaction(session.id)
    assert ready is not None
    assert ready.step == "ready"
    assert ready.prompt_message_id == 600
    _, message_id, text, buttons = bot.edited[0]
    assert message_id == 600
    assert "Batch move result" in text
    assert "Choose a directory again to retry" in text
    assert isinstance(buttons, list)
    assert [button.text for button in buttons[0]] == ["Retry move", "Cancel"]


async def test_bootstrap_pending_recovery_isolates_unauthorized_and_ambiguous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(tmp_path)
    queue, healthy_job = await seed_completed(config, message_id=5200)
    _, broken_job = await seed_completed(config, message_id=5201)
    _, unauthorized_job = await seed_completed(config, requester_id=2002, message_id=5202)
    healthy = await queue.create_interaction(
        1001,
        "archive",
        "result-pending",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=healthy_job,  # type: ignore[arg-type]
        prompt_message_id=610,
    )
    broken = await queue.create_interaction(
        1001,
        "rename",
        "result-pending",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=broken_job,  # type: ignore[arg-type]
        prompt_message_id=620,
    )
    unauthorized = await queue.create_interaction(
        2002,
        "archive",
        "result-pending",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=unauthorized_job,  # type: ignore[arg-type]
        prompt_message_id=630,
    )
    from tgharbor.core.library_api import LibraryAPI

    async def forbidden_mutation(*args: object, **kwargs: object) -> object:
        raise AssertionError("recovery must not call a library mutation API")

    monkeypatch.setattr(LibraryAPI, "organize", forbidden_mutation)
    monkeypatch.setattr(LibraryAPI, "organize_batch", forbidden_mutation)
    monkeypatch.setattr(LibraryAPI, "mkdir", forbidden_mutation)

    bot = FakeBotClient()
    original_edit = bot.edit_message

    async def selectively_failing_edit(
        actor_id: int,
        message_id: int,
        text: str,
        *,
        buttons: object | None = None,
    ) -> SimpleNamespace:
        if message_id == 620:
            raise TimeoutError("ambiguous")
        return await original_edit(actor_id, message_id, text, buttons=buttons)

    bot.edit_message = selectively_failing_edit  # type: ignore[method-assign]

    await run(config, clients=TelegramClients(bot=bot, user=FakeUserClient()))

    assert await queue.get_interaction(healthy.id) is None
    retained_broken = await queue.get_interaction(broken.id)
    assert retained_broken is not None and retained_broken.step == "result-pending"
    retained_unauthorized = await queue.get_interaction(unauthorized.id)
    assert retained_unauthorized is not None and retained_unauthorized.step == "result-pending"
    edited_ids = [message_id for _, message_id, _, _ in bot.edited]
    assert edited_ids == [610]
    assert all(requester != 2002 for requester, _, _ in bot.sent)


async def test_bootstrap_pending_result_fallback_send_binds_and_cleans(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    queue, job_id = await seed_completed(config, message_id=5300)
    session = await queue.create_interaction(
        1001,
        "archive",
        "result-pending",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        job_id=job_id,  # type: ignore[arg-type]
        prompt_message_id=555,
    )
    bot = FakeBotClient()
    bot.edit_fail_ids = {1001}
    bot.edit_error = MessageIdInvalidError(None)

    await run(config, clients=TelegramClients(bot=bot, user=FakeUserClient()))

    assert await queue.get_interaction(session.id) is None
    result_indexes = [index for index, (_, text, _) in enumerate(bot.sent) if "Completed" in text]
    assert len(result_indexes) == 1
    _, result_text, result_buttons = bot.sent[result_indexes[0]]
    assert "Completed" in result_text
    assert result_buttons is None
    assert bot.edited == []


async def test_bootstrap_recovers_all_pending_rows_beyond_one_hundred(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    expiry = datetime.now(UTC) + timedelta(minutes=5)
    last_queue: SQLiteJobQueue | None = None
    for index in range(105):
        queue, job_id = await seed_completed(config, message_id=6000 + index)
        await queue.create_interaction(
            1001,
            "archive",
            "result-pending",
            expires_at=expiry,
            job_id=job_id,  # type: ignore[arg-type]
            prompt_message_id=2000 + index,
        )
        last_queue = queue
    assert last_queue is not None

    bot = FakeBotClient()
    attempted: list[int] = []
    original_edit = bot.edit_message

    async def selectively_failing_edit(
        actor_id: int,
        message_id: int,
        text: str,
        *,
        buttons: object | None = None,
    ) -> SimpleNamespace:
        attempted.append(message_id)
        if message_id == 2050:
            raise TimeoutError("ambiguous")
        return await original_edit(actor_id, message_id, text, buttons=buttons)

    bot.edit_message = selectively_failing_edit  # type: ignore[method-assign]

    await run(config, clients=TelegramClients(bot=bot, user=FakeUserClient()))

    # Every row, including rows past the former one-hundred-row boundary, is
    # attempted in the same startup and the single failure stays isolated.
    assert set(attempted) == set(range(2000, 2105))
    assert attempted.index(2104) > attempted.index(2050)
    retained = await last_queue.list_pending_delivery_interactions()
    assert [session.prompt_message_id for session in retained] == [2050]
    assert len(bot.edited) == 104


async def test_bootstrap_recovers_expired_working_confirm_with_renewed_expiry(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    queue, job_id = await seed_completed(config, message_id=6100)
    session = await queue.create_interaction(
        1001,
        "archive",
        "working-confirm",
        expires_at=datetime.now(UTC) - timedelta(hours=2),
        job_id=job_id,  # type: ignore[arg-type]
        pending_name="clip.mp4",
        pending_directory="videos",
        prompt_message_id=650,
    )
    bot = FakeBotClient()

    await run(config, clients=TelegramClients(bot=bot, user=FakeUserClient()))

    restored = await queue.get_interaction(session.id)
    assert restored is not None
    assert restored.step == "confirm"
    assert restored.prompt_message_id == 650
    assert restored.pending_directory == "videos"
    assert restored.expires_at > datetime.now(UTC)
    _, message_id, text, buttons = bot.edited[0]
    assert message_id == 650
    assert "Confirm file organization:" in text
    assert isinstance(buttons, list)
    assert [button.text for button in buttons[0]] == ["Move here", "Back", "Cancel"]
