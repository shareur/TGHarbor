import pytest

from tgharbor.core.jobs import NewDownloadJob, SourceSession


def test_new_job_has_stable_dedupe_key() -> None:
    job = NewDownloadJob(
        requester_id=42,
        source_session=SourceSession.BOT,
        source_chat="42",
        source_message_id=7,
    )

    assert job.dedupe_key == "42:bot:42:7"


@pytest.mark.parametrize("field", ["requester_id", "source_message_id", "max_attempts"])
def test_new_job_requires_positive_numbers(field: str) -> None:
    values = {
        "requester_id": 42,
        "source_session": SourceSession.USER,
        "source_chat": "channel",
        "source_message_id": 7,
        "max_attempts": 3,
    }
    values[field] = 0

    with pytest.raises(ValueError, match="positive"):
        NewDownloadJob(**values)
