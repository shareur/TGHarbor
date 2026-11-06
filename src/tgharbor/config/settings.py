from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when the application configuration is invalid."""


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    api_id: int
    api_hash: str = field(repr=False)
    bot_token: str = field(repr=False)
    authorized_users: tuple[int, ...]
    phone: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class DownloadsConfig:
    path: Path
    concurrency: int = 2
    timeout_seconds: int = 3600
    max_attempts: int = 3
    retry_base_seconds: int = 5


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    path: Path


@dataclass(frozen=True, slots=True)
class SessionsConfig:
    path: Path


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    level: str = "INFO"


@dataclass(frozen=True, slots=True)
class AppConfig:
    telegram: TelegramConfig
    downloads: DownloadsConfig
    database: DatabaseConfig
    sessions: SessionsConfig
    logging: LoggingConfig
    source: Path


_ROOT_KEYS = {"telegram", "downloads", "database", "sessions", "logging"}
_SECTION_KEYS = {
    "telegram": {"api_id", "api_hash", "bot_token", "authorized_users", "phone"},
    "downloads": {
        "path",
        "concurrency",
        "timeout_seconds",
        "max_attempts",
        "retry_base_seconds",
    },
    "database": {"path"},
    "sessions": {"path"},
    "logging": {"level"},
}
_ENV_OVERRIDES = {
    "TGHARBOR_TELEGRAM_API_ID": ("telegram", "api_id"),
    "TGHARBOR_TELEGRAM_API_HASH": ("telegram", "api_hash"),
    "TGHARBOR_TELEGRAM_BOT_TOKEN": ("telegram", "bot_token"),
    "TGHARBOR_TELEGRAM_PHONE": ("telegram", "phone"),
    "TGHARBOR_TELEGRAM_AUTHORIZED_USERS": ("telegram", "authorized_users"),
    "TGHARBOR_DOWNLOADS_PATH": ("downloads", "path"),
    "TGHARBOR_DOWNLOADS_CONCURRENCY": ("downloads", "concurrency"),
    "TGHARBOR_DOWNLOADS_TIMEOUT_SECONDS": ("downloads", "timeout_seconds"),
    "TGHARBOR_DOWNLOADS_MAX_ATTEMPTS": ("downloads", "max_attempts"),
    "TGHARBOR_DOWNLOADS_RETRY_BASE_SECONDS": ("downloads", "retry_base_seconds"),
    "TGHARBOR_DATABASE_PATH": ("database", "path"),
    "TGHARBOR_SESSIONS_PATH": ("sessions", "path"),
    "TGHARBOR_LOG_LEVEL": ("logging", "level"),
}
_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}


def load_config(
    path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> AppConfig:
    """Load a YAML configuration file and apply supported environment overrides."""
    environment = os.environ if environ is None else environ
    configured_path = (
        path if path is not None else environment.get("TGHARBOR_CONFIG", "config.yaml")
    )
    source = Path(configured_path).resolve(strict=False)
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ConfigError(f"configuration file not found: {source}") from error
    except OSError as error:
        raise ConfigError(f"cannot read configuration file: {source}") from error
    except yaml.YAMLError as error:
        raise ConfigError(f"invalid YAML in configuration file: {source}") from error

    root = _as_mapping(raw, "configuration")
    _reject_unknown(root, _ROOT_KEYS, "configuration")
    sections = {name: dict(_as_mapping(root.get(name, {}), name)) for name in _ROOT_KEYS}
    for name, values in sections.items():
        _reject_unknown(values, _SECTION_KEYS[name], name)
    _apply_environment(sections, environment)

    base = source.parent
    telegram = sections["telegram"]
    downloads = sections["downloads"]
    database = sections["database"]
    sessions = sections["sessions"]
    logging = sections["logging"]

    api_id = _positive_int(telegram.get("api_id"), "telegram.api_id")
    api_hash = _required_string(telegram.get("api_hash"), "telegram.api_hash")
    bot_token = _required_string(telegram.get("bot_token"), "telegram.bot_token")
    authorized_users = _authorized_users(
        telegram.get("authorized_users"), "telegram.authorized_users"
    )
    phone = _optional_string(telegram.get("phone"), "telegram.phone")
    level = _required_string(logging.get("level", "INFO"), "logging.level").upper()
    if level not in _LOG_LEVELS:
        raise ConfigError("logging.level must be one of: CRITICAL, ERROR, WARNING, INFO, DEBUG")

    return AppConfig(
        telegram=TelegramConfig(
            api_id=api_id,
            api_hash=api_hash,
            bot_token=bot_token,
            authorized_users=authorized_users,
            phone=phone,
        ),
        downloads=DownloadsConfig(
            path=_config_path(downloads.get("path", "./downloads"), base, "downloads.path"),
            concurrency=_positive_int(downloads.get("concurrency", 2), "downloads.concurrency"),
            timeout_seconds=_positive_int(
                downloads.get("timeout_seconds", 3600), "downloads.timeout_seconds"
            ),
            max_attempts=_positive_int(downloads.get("max_attempts", 3), "downloads.max_attempts"),
            retry_base_seconds=_positive_int(
                downloads.get("retry_base_seconds", 5), "downloads.retry_base_seconds"
            ),
        ),
        database=DatabaseConfig(
            path=_config_path(database.get("path", "./data/tgharbor.db"), base, "database.path")
        ),
        sessions=SessionsConfig(
            path=_config_path(sessions.get("path", "./data/sessions"), base, "sessions.path")
        ),
        logging=LoggingConfig(level=level),
        source=source,
    )


def _as_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{field_name} must be a mapping")
    if not all(isinstance(key, str) for key in value):
        raise ConfigError(f"{field_name} keys must be strings")
    return value


def _reject_unknown(values: Mapping[str, Any], allowed: set[str], section: str) -> None:
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ConfigError(f"unknown {section} setting(s): {', '.join(unknown)}")


def _apply_environment(sections: dict[str, dict[str, Any]], environment: Mapping[str, str]) -> None:
    for variable, (section, key) in _ENV_OVERRIDES.items():
        if variable in environment:
            sections[section][key] = environment[variable]


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{field_name} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ConfigError(f"{field_name} must be a positive integer") from error
    if result <= 0 or (isinstance(value, float) and not value.is_integer()):
        raise ConfigError(f"{field_name} must be a positive integer")
    return result


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, field_name)


def _authorized_users(value: Any, field_name: str) -> tuple[int, ...]:
    raw_users: Any = value.split(",") if isinstance(value, str) else value
    if not isinstance(raw_users, list) or not raw_users:
        raise ConfigError(f"{field_name} must contain at least one user ID")
    users = tuple(_positive_int(item, field_name) for item in raw_users)
    if len(set(users)) != len(users):
        raise ConfigError(f"{field_name} must not contain duplicate user IDs")
    return users


def _config_path(value: Any, base: Path, field_name: str) -> Path:
    text = _required_string(value, field_name)
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve(strict=False)
