from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence

from tgharbor.app.bootstrap import run
from tgharbor.config import load_config
from tgharbor.telegram.sessions import login_user

_TELETHON_LOGGER = "telethon"


def configure_logging(level: str) -> None:
    """Configure TGHarbor logging and limit the Telethon namespace to ERROR."""
    root_level = getattr(logging, level)
    logging.basicConfig(
        level=root_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger().setLevel(root_level)
    telethon_logger = logging.getLogger(_TELETHON_LOGGER)
    telethon_logger.setLevel(max(root_level, logging.ERROR))
    telethon_logger.propagate = True


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="TGHarbor Core Downloader")
    parser.add_argument("command", nargs="?", choices=("run", "login"), default="run")
    parser.add_argument(
        "--config", help="configuration file (default: TGHARBOR_CONFIG or config.yaml)"
    )
    args = parser.parse_args(argv)
    config = load_config(args.config)
    configure_logging(config.logging.level)
    if args.command == "login":
        asyncio.run(login_user(config))
    else:
        asyncio.run(run(config))


if __name__ == "__main__":
    main()
