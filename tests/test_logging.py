from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from tgharbor.main import configure_logging

TELETHON_LOGGER = "telethon"
TELETHON_CHILD_LOGGER = "telethon.network.mtprotosender"
WORKER_LOGGER = "tgharbor.core.worker"
SNAPSHOT_LOGGERS = (TELETHON_LOGGER, TELETHON_CHILD_LOGGER, WORKER_LOGGER)


class RecordingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def snapshot_logging_state() -> dict[str, object]:
    root = logging.getLogger()
    loggers = {}
    for name in SNAPSHOT_LOGGERS:
        logger = logging.getLogger(name)
        loggers[name] = (logger.level, logger.propagate, logger.disabled)
    return {
        "root_level": root.level,
        "root_handlers": list(root.handlers),
        "loggers": loggers,
    }


def restore_logging_state(state: dict[str, object]) -> None:
    root = logging.getLogger()
    root.setLevel(state["root_level"])
    root.handlers[:] = state["root_handlers"]
    loggers = state["loggers"]
    assert isinstance(loggers, dict)
    for name, values in loggers.items():
        logger = logging.getLogger(name)
        level, propagate, disabled = values
        logger.setLevel(level)
        logger.propagate = propagate
        logger.disabled = disabled


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    state = snapshot_logging_state()
    try:
        yield
    finally:
        restore_logging_state(state)


def test_tgharbor_root_level_is_configured() -> None:
    configure_logging("INFO")

    assert logging.getLogger().level == logging.INFO
    assert logging.getLogger(WORKER_LOGGER).getEffectiveLevel() == logging.INFO


def test_telethon_namespace_and_children_are_error_only() -> None:
    configure_logging("INFO")

    telethon_logger = logging.getLogger(TELETHON_LOGGER)
    assert telethon_logger.level == logging.ERROR
    assert telethon_logger.propagate is True
    assert logging.getLogger(TELETHON_CHILD_LOGGER).getEffectiveLevel() == logging.ERROR


def test_telethon_info_and_warning_are_suppressed_but_error_is_emitted() -> None:
    configure_logging("INFO")
    telethon_logger = logging.getLogger(TELETHON_LOGGER)
    handler = RecordingHandler()
    telethon_logger.addHandler(handler)
    try:
        telethon_logger.info("suppressed info")
        telethon_logger.warning("suppressed warning")
        telethon_logger.error("emitted error")
    finally:
        telethon_logger.removeHandler(handler)

    assert [record.levelno for record in handler.records] == [logging.ERROR]
    assert handler.records[0].getMessage() == "emitted error"


def test_stricter_root_level_keeps_telethon_at_critical() -> None:
    configure_logging("CRITICAL")
    telethon_logger = logging.getLogger(TELETHON_LOGGER)
    handler = RecordingHandler()
    telethon_logger.addHandler(handler)
    try:
        telethon_logger.error("suppressed error")
        telethon_logger.critical("emitted critical")
    finally:
        telethon_logger.removeHandler(handler)

    assert telethon_logger.level == logging.CRITICAL
    assert [record.levelno for record in handler.records] == [logging.CRITICAL]


def test_logging_state_can_be_restored_between_tests() -> None:
    before = snapshot_logging_state()
    try:
        configure_logging("DEBUG")
        assert logging.getLogger(TELETHON_LOGGER).level == logging.ERROR
        restore_logging_state(before)
        assert snapshot_logging_state() == before
    finally:
        restore_logging_state(before)
