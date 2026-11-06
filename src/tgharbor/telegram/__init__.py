"""Telegram integration."""

from tgharbor.telegram.links import MessageReference, MessageReferenceError, parse_message_link
from tgharbor.telegram.provider import TelethonMediaProvider

__all__ = [
    "MessageReference",
    "MessageReferenceError",
    "TelethonMediaProvider",
    "parse_message_link",
]
