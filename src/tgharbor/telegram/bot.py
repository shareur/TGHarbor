from __future__ import annotations

from typing import Any
from uuid import UUID

from telethon import events

from tgharbor.core.job_api import JobAPI, JobNotFoundError, UnauthorizedUserError
from tgharbor.core.jobs import DownloadJob, JobStatus, SourceSession
from tgharbor.telegram.links import MessageReferenceError, parse_message_link

_HELP = (
    "Send or forward one media message, or send one https://t.me/... message link.\n"
    "Commands:\n"
    "/status [job-id] — show recent jobs or one job\n"
    "/cancel job-id — cancel a pending or running job\n"
    "/help — show this help"
)


class TelegramBotController:
    def __init__(self, client: Any, jobs: JobAPI) -> None:
        self.client = client
        self.jobs = jobs
        self._handler = events.NewMessage(incoming=True)

    def start(self) -> None:
        self.client.add_event_handler(self.handle_event, self._handler)

    def stop(self) -> None:
        self.client.remove_event_handler(self.handle_event, self._handler)

    async def handle_event(self, event: Any) -> None:
        if not getattr(event, "is_private", True):
            return
        requester_id = event.sender_id
        if requester_id is None:
            return
        try:
            self.jobs.require_authorized(requester_id)
        except UnauthorizedUserError:
            await event.respond("Unauthorized.")
            return

        text = (event.raw_text or "").strip()
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text else ""
        if command in {"/start", "/help"}:
            await event.respond(_HELP)
            return
        if command == "/status":
            await self._status(event, requester_id, text)
            return
        if command == "/cancel":
            await self._cancel(event, requester_id, text)
            return
        try:
            reference = parse_message_link(text)
        except MessageReferenceError as error:
            if getattr(event.message, "file", None) is None:
                await event.respond(f"Cannot create job: {error}\n\n{_HELP}")
                return
            job = await self.jobs.submit(
                requester_id,
                SourceSession.BOT,
                str(event.chat_id),
                event.id,
            )
            await event.respond(_accepted(job))
            return
        job = await self.jobs.submit(
            requester_id,
            SourceSession.USER,
            reference.chat,
            reference.message_id,
        )
        await event.respond(_accepted(job))

    async def _status(self, event: Any, requester_id: int, text: str) -> None:
        parts = text.split()
        if len(parts) > 2:
            await event.respond("Usage: /status [job-id]")
            return
        if len(parts) == 2:
            job_id = _parse_uuid(parts[1])
            if job_id is None:
                await event.respond("Invalid job ID.")
                return
            try:
                job = await self.jobs.get(requester_id, job_id)
            except JobNotFoundError:
                await event.respond("Job not found.")
                return
            await event.respond(_format_job(job))
            return
        jobs = await self.jobs.latest(requester_id)
        await event.respond(
            "No jobs yet."
            if not jobs
            else "Recent jobs:\n" + "\n".join(_format_job(job) for job in jobs)
        )

    async def _cancel(self, event: Any, requester_id: int, text: str) -> None:
        parts = text.split()
        if len(parts) != 2 or (job_id := _parse_uuid(parts[1])) is None:
            await event.respond("Usage: /cancel job-id")
            return
        try:
            job = await self.jobs.cancel(requester_id, job_id)
        except JobNotFoundError:
            await event.respond("Job not found.")
            return
        await event.respond(_format_job(job))


class TelegramJobNotifier:
    def __init__(self, bot_client: Any) -> None:
        self.bot_client = bot_client

    async def job_finished(self, job: DownloadJob) -> None:
        await self.bot_client.send_message(job.requester_id, _format_job(job))


def _accepted(job: DownloadJob) -> str:
    return f"Job accepted: {job.id}\nStatus: {job.status.value}"


def _format_job(job: DownloadJob) -> str:
    progress = f"{job.progress_bytes} bytes"
    if job.total_bytes:
        percentage = min(100, int(job.progress_bytes * 100 / job.total_bytes))
        progress = f"{percentage}% ({job.progress_bytes}/{job.total_bytes} bytes)"
    lines = [f"{job.id} — {job.status.value}", f"Progress: {progress}"]
    if job.status is JobStatus.FAILED and job.error:
        lines.append(f"Error: {job.error}")
    return "\n".join(lines)


def _parse_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None
