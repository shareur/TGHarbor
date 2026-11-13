from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit


class MessageReferenceError(ValueError):
    """Raised when text is not one supported Telegram message link."""


@dataclass(frozen=True, slots=True)
class MessageReference:
    chat: str
    message_id: int


_PUBLIC_PATH = re.compile(r"^/([A-Za-z0-9_]{5,32})/([1-9][0-9]*)/?$")
_PRIVATE_PATH = re.compile(r"^/c/([1-9][0-9]*)/([1-9][0-9]*)/?$")


def parse_message_link(text: str) -> MessageReference:
    """Parse one public or private Telegram message link without accepting ranges."""
    value = text.strip()
    if any(character.isspace() for character in value):
        raise MessageReferenceError("send exactly one Telegram message link")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise MessageReferenceError("malformed Telegram message link") from error
    if parsed.scheme != "https" or hostname not in {"t.me", "www.t.me", "telegram.me"}:
        raise MessageReferenceError("link must use https://t.me/")
    if parsed.query or parsed.fragment or parsed.username or parsed.password or port:
        raise MessageReferenceError("link contains unsupported components")

    private = _PRIVATE_PATH.fullmatch(parsed.path)
    if private:
        return MessageReference(chat=f"-100{private.group(1)}", message_id=int(private.group(2)))
    public = _PUBLIC_PATH.fullmatch(parsed.path)
    if public:
        return MessageReference(chat=public.group(1), message_id=int(public.group(2)))
    raise MessageReferenceError("only single public or private message links are supported")
