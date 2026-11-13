from __future__ import annotations

import asyncio
import getpass
from dataclasses import dataclass
from pathlib import Path

from telethon import TelegramClient

from tgharbor.config import AppConfig


class SessionAuthorizationError(RuntimeError):
    """Raised when the persistent user session has not been authorized."""


@dataclass(slots=True)
class TelegramClients:
    bot: TelegramClient
    user: TelegramClient

    async def disconnect(self) -> None:
        await asyncio.gather(self.bot.disconnect(), self.user.disconnect(), return_exceptions=True)


def build_clients(config: AppConfig) -> TelegramClients:
    session_path = _prepare_session_directory(config.sessions.path)
    common = (config.telegram.api_id, config.telegram.api_hash)
    return TelegramClients(
        bot=TelegramClient(session_path / "bot", *common),
        user=TelegramClient(session_path / "user", *common),
    )


async def start_service_clients(config: AppConfig, clients: TelegramClients) -> None:
    """Start the Bot and reuse an already-authorized user session."""
    try:
        await clients.bot.start(bot_token=config.telegram.bot_token)
        await clients.user.connect()
        if not await clients.user.is_user_authorized():
            raise SessionAuthorizationError(
                "user session is not authorized; run `tgharbor login` in a trusted terminal"
            )
    except BaseException:
        await clients.disconnect()
        raise


async def login_user(config: AppConfig) -> None:
    """Authorize the user session through local terminal prompts."""
    session_path = _prepare_session_directory(config.sessions.path)
    client = TelegramClient(session_path / "user", config.telegram.api_id, config.telegram.api_hash)
    phone = config.telegram.phone or input("Telegram phone number: ").strip()
    if not phone:
        raise SessionAuthorizationError("a phone number is required for user login")
    try:
        await client.start(
            phone=phone,
            code_callback=lambda: input("Telegram verification code: ").strip(),
            password=lambda: getpass.getpass("Telegram two-step verification password: "),
        )
        if not await client.is_user_authorized():
            raise SessionAuthorizationError("Telegram did not authorize the user session")
    finally:
        await client.disconnect()


def _prepare_session_directory(path: Path) -> Path:
    if path.is_symlink():
        raise SessionAuthorizationError(f"unsafe session directory: {path}")
    resolved = path.resolve(strict=False)
    resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved.chmod(0o700)
    if not resolved.is_dir():
        raise SessionAuthorizationError(f"unsafe session directory: {resolved}")
    return resolved
