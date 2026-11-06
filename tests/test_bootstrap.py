from __future__ import annotations

from pathlib import Path

import pytest

from tgharbor.app.bootstrap import run
from tgharbor.app.instance_lock import InstanceAlreadyRunningError, InstanceLock
from tgharbor.config import AppConfig, load_config
from tgharbor.telegram.sessions import SessionAuthorizationError, TelegramClients


class FakeBotClient:
    def __init__(self) -> None:
        self.started = False
        self.disconnected = False
        self.handlers: list[tuple[object, object]] = []

    async def start(self, *, bot_token: str) -> None:
        self.started = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def run_until_disconnected(self) -> None:
        return None

    def add_event_handler(self, callback: object, event: object) -> None:
        self.handlers.append((callback, event))

    def remove_event_handler(self, callback: object, event: object) -> None:
        self.handlers.remove((callback, event))

    async def send_message(self, requester_id: int, text: str) -> None:
        pass


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


def make_config(tmp_path: Path) -> AppConfig:
    source = tmp_path / "config.yaml"
    source.write_text(
        """
telegram:
  api_id: 12345
  api_hash: test-hash
  bot_token: test-token
  authorized_users: [1001]
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
