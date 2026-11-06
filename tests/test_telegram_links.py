import pytest

from tgharbor.telegram.links import MessageReference, MessageReferenceError, parse_message_link


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        ("https://t.me/example_channel/123", MessageReference("example_channel", 123)),
        ("https://telegram.me/example_channel/123/", MessageReference("example_channel", 123)),
        ("https://t.me/c/123456789/42", MessageReference("-100123456789", 42)),
    ],
)
def test_parse_single_message_link(link: str, expected: MessageReference) -> None:
    assert parse_message_link(link) == expected


@pytest.mark.parametrize(
    "link",
    [
        "http://t.me/example_channel/1",
        "https://evil.example/example_channel/1",
        "https://t.me/example_channel",
        "https://t.me/example_channel/1?single=true",
        "https://t.me/example_channel/1 https://t.me/example_channel/2",
        "https://t.me/c/123/1/2",
        "https://t.me/s/example_channel/1",
        "https://t.me:invalid/example_channel/1",
    ],
)
def test_reject_unsupported_or_multiple_links(link: str) -> None:
    with pytest.raises(MessageReferenceError):
        parse_message_link(link)
