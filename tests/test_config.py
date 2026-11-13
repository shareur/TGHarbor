from __future__ import annotations

from pathlib import Path

import pytest

from tgharbor.config import ConfigError, load_config


def write_config(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def valid_config() -> str:
    return """
telegram:
  api_id: 12345
  api_hash: file-api-hash
  bot_token: file-bot-token
  phone: "+12025550123"
  authorized_users: [1001, 1002]
downloads:
  path: downloads
  concurrency: 4
  timeout_seconds: 600
  max_attempts: 5
  retry_base_seconds: 2
database:
  path: state/tgharbor.db
sessions:
  path: state/sessions
logging:
  level: warning
"""


def test_loads_and_resolves_relative_paths(tmp_path: Path) -> None:
    source = write_config(tmp_path / "config.yaml", valid_config())

    config = load_config(source, environ={})

    assert config.source == source
    assert config.telegram.api_id == 12345
    assert config.telegram.authorized_users == (1001, 1002)
    assert config.downloads.path == tmp_path / "downloads"
    assert config.database.path == tmp_path / "state/tgharbor.db"
    assert config.sessions.path == tmp_path / "state/sessions"
    assert config.logging.level == "WARNING"


def test_environment_overrides_file_without_leaking_secrets(tmp_path: Path) -> None:
    source = write_config(tmp_path / "config.yaml", valid_config())
    environment = {
        "TGHARBOR_TELEGRAM_API_ID": "67890",
        "TGHARBOR_TELEGRAM_API_HASH": "environment-api-hash",
        "TGHARBOR_TELEGRAM_BOT_TOKEN": "environment-bot-token",
        "TGHARBOR_TELEGRAM_AUTHORIZED_USERS": "2001,2002",
        "TGHARBOR_DOWNLOADS_PATH": "/srv/downloads",
        "TGHARBOR_DOWNLOADS_CONCURRENCY": "8",
        "TGHARBOR_DATABASE_PATH": "/srv/data/tgharbor.db",
        "TGHARBOR_SESSIONS_PATH": "/srv/data/sessions",
    }

    config = load_config(source, environ=environment)

    assert config.telegram.api_id == 67890
    assert config.telegram.api_hash == "environment-api-hash"
    assert config.telegram.bot_token == "environment-bot-token"
    assert config.telegram.authorized_users == (2001, 2002)
    assert config.downloads.path == Path("/srv/downloads")
    assert config.downloads.concurrency == 8
    assert config.database.path == Path("/srv/data/tgharbor.db")
    assert config.sessions.path == Path("/srv/data/sessions")
    rendered = repr(config)
    assert "environment-api-hash" not in rendered
    assert "environment-bot-token" not in rendered


def test_config_path_can_come_from_environment(tmp_path: Path) -> None:
    source = write_config(tmp_path / "custom.yaml", valid_config())

    config = load_config(environ={"TGHARBOR_CONFIG": str(source)})

    assert config.source == source


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ("api_id: 12345", "telegram.api_hash"),
        ("api_id: false\n  api_hash: hash", "telegram.api_id"),
        ("authorized_users: []", "telegram.authorized_users"),
        ("authorized_users: [1001, 1001]", "duplicate"),
        ("authorized_users: [1001]\n  surprise: true", "unknown telegram"),
    ],
)
def test_rejects_invalid_telegram_configuration(
    tmp_path: Path, replacement: str, message: str
) -> None:
    content = valid_config().replace("api_id: 12345\n  api_hash: file-api-hash", replacement)
    if replacement.startswith("authorized_users"):
        content = valid_config().replace("authorized_users: [1001, 1002]", replacement)
    source = write_config(tmp_path / "config.yaml", content)

    with pytest.raises(ConfigError, match=message):
        load_config(source, environ={})


def test_errors_do_not_include_secret_values(tmp_path: Path) -> None:
    source = write_config(tmp_path / "config.yaml", valid_config())

    with pytest.raises(ConfigError) as captured:
        load_config(
            source,
            environ={
                "TGHARBOR_TELEGRAM_API_HASH": "do-not-log-api-hash",
                "TGHARBOR_TELEGRAM_BOT_TOKEN": "do-not-log-bot-token",
                "TGHARBOR_DOWNLOADS_CONCURRENCY": "invalid",
            },
        )

    message = str(captured.value)
    assert "do-not-log-api-hash" not in message
    assert "do-not-log-bot-token" not in message


def test_missing_file_has_clear_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="configuration file not found"):
        load_config(tmp_path / "missing.yaml", environ={})
