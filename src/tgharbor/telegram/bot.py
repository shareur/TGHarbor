from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from telethon import Button, events

from tgharbor.core.job_api import JobAPI, JobNotFoundError, UnauthorizedUserError
from tgharbor.core.jobs import (
    DownloadJob,
    InteractionSession,
    JobStatus,
    SourceSession,
    StoredFile,
)
from tgharbor.core.library_api import LibraryAPI, StoredFileNotFoundError
from tgharbor.telegram.links import MessageReferenceError, parse_message_link

_HELP = (
    "Send or forward media, or send one https://t.me/... message link.\n"
    "Job commands:\n"
    "/status [job-id] — active jobs or one requester-owned job\n"
    "/history [page] — requester-owned completed/failed/cancelled jobs\n"
    "/cancel job-id — cancel a pending or running job\n"
    "Shared library commands:\n"
    "/files [page] · /dirs [page] · /stats\n"
    "/rename file-id new-name\n"
    "/archive file-id directory\n"
    "/mkdir directory\n"
    "/help — show this help"
)
_INTERACTION_LIFETIME = timedelta(hours=24)


class InteractionRepository(Protocol):
    async def create_interaction(
        self,
        actor_id: int,
        action: str,
        step: str,
        *,
        expires_at: datetime,
        job_id: UUID | None = None,
        pending_name: str | None = None,
        pending_directory: str | None = None,
    ) -> InteractionSession: ...

    async def get_interaction(self, interaction_id: UUID) -> InteractionSession | None: ...

    async def latest_interaction_for_actor(self, actor_id: int) -> InteractionSession | None: ...

    async def update_interaction(
        self,
        interaction_id: UUID,
        actor_id: int,
        *,
        step: str,
        pending_name: str | None = None,
        pending_directory: str | None = None,
    ) -> InteractionSession: ...

    async def delete_interaction(self, interaction_id: UUID, actor_id: int) -> bool: ...

    async def delete_interactions_for_job(self, job_id: UUID) -> int: ...


class TelegramBotController:
    def __init__(
        self,
        client: Any,
        jobs: JobAPI,
        library: LibraryAPI | None = None,
        interactions: InteractionRepository | None = None,
    ) -> None:
        self.client = client
        self.jobs = jobs
        self.library = library
        self.interactions = interactions
        self._message_handler = events.NewMessage(incoming=True)
        self._callback_handler = events.CallbackQuery()

    def start(self) -> None:
        self.client.add_event_handler(self.handle_event, self._message_handler)
        if self.interactions is not None:
            self.client.add_event_handler(self.handle_callback, self._callback_handler)

    def stop(self) -> None:
        self.client.remove_event_handler(self.handle_event, self._message_handler)
        if self.interactions is not None:
            self.client.remove_event_handler(self.handle_callback, self._callback_handler)

    async def handle_event(self, event: Any) -> None:
        if not getattr(event, "is_private", True):
            return
        requester_id = event.sender_id
        if requester_id is None or not await self._authorize(event, requester_id):
            return

        text = (event.raw_text or "").strip()
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text else ""
        handlers = {
            "/status": self._status,
            "/history": self._history,
            "/cancel": self._cancel,
            "/files": self._files,
            "/dirs": self._dirs,
            "/stats": self._stats,
            "/rename": self._rename,
            "/archive": self._archive,
            "/mkdir": self._mkdir,
        }
        if command in {"/start", "/help"}:
            await event.respond(_HELP)
            return
        if command in handlers:
            await handlers[command](event, requester_id, text)
            return
        if not command.startswith("/") and await self._continue_interaction(
            event, requester_id, text
        ):
            return
        await self._submit(event, requester_id, text)

    async def handle_callback(self, event: Any) -> None:
        actor_id = event.sender_id
        if actor_id is None or not await self._authorize(event, actor_id):
            return
        parsed = _parse_callback(getattr(event, "data", b""))
        if parsed is None or self.interactions is None or self.library is None:
            await _callback_reply(event, "This action is invalid or unavailable.")
            return
        interaction_id, token = parsed
        session = await self.interactions.get_interaction(interaction_id)
        if (
            session is None
            or session.actor_id != actor_id
            or session.expires_at <= datetime.now(UTC)
            or not _valid_callback_token(session, token)
        ):
            await _callback_reply(event, "This action is unauthorized, expired, or stale.")
            return
        if token == "cancel":
            await self.interactions.delete_interaction(session.id, actor_id)
            await _callback_reply(event, "Action cancelled.")
            return
        if token == "new-directory":
            await self.interactions.update_interaction(
                session.id,
                actor_id,
                step="await-new-directory",
                pending_name=session.pending_name,
            )
            await _callback_reply(event, "Send the new archive directory path.")
            return
        if token.startswith("directory:"):
            await self._select_directory(event, actor_id, session, token)
            return
        if session.action in {"files-page", "history-page"}:
            try:
                page = int(session.step)
            except ValueError:
                await _callback_reply(event, "This page action is stale.")
                return
            await self.interactions.delete_interaction(session.id, actor_id)
            if session.action == "files-page":
                await self._render_files(event, actor_id, page)
            else:
                await self._render_history(event, actor_id, page)
            return
        if session.job_id is None:
            await _callback_reply(event, "The target file is no longer available.")
            return
        try:
            await self.library.resolve(actor_id, session.job_id.hex)
        except StoredFileNotFoundError:
            await _callback_reply(event, "The target file is no longer available.")
            return
        if token == "confirm":
            await self._confirm_interaction(event, actor_id, session)
            return
        if session.action == "keep":
            await self.interactions.delete_interactions_for_job(session.job_id)
            await _callback_reply(event, "Kept in Inbox.")
            return
        step = (
            "await-name"
            if session.action in {"rename", "rename-and-archive"}
            else "await-directory"
        )
        updated = await self.interactions.update_interaction(session.id, actor_id, step=step)
        if step == "await-name":
            await _callback_reply(event, "Send the new filename.")
        else:
            await self._prompt_directories(event, actor_id, updated)

    async def _authorize(self, event: Any, actor_id: int) -> bool:
        try:
            self.jobs.require_authorized(actor_id)
        except UnauthorizedUserError:
            await event.respond("Unauthorized.")
            return False
        return True

    async def _submit(self, event: Any, requester_id: int, text: str) -> None:
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
        else:
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
            try:
                job = await self.jobs.resolve(requester_id, parts[1])
            except JobNotFoundError:
                await event.respond("Job not found or ID is ambiguous.")
                return
            await event.respond(_format_job(job))
            return
        jobs = await self.jobs.active(requester_id)
        await event.respond(
            "No active jobs."
            if not jobs
            else "Active jobs:\n" + "\n\n".join(_format_job(job) for job in jobs)
        )

    async def _history(self, event: Any, requester_id: int, text: str) -> None:
        page = _parse_page(text)
        if page is None:
            await event.respond("Usage: /history [page]")
            return
        await self._render_history(event, requester_id, page)

    async def _render_history(self, event: Any, requester_id: int, page: int) -> None:
        jobs = await self.jobs.history(requester_id, limit=11, offset=(page - 1) * 10)
        visible = jobs[:10]
        buttons = await self._page_buttons(
            requester_id, "history-page", page, has_next=len(jobs) > 10
        )
        await event.respond(
            f"No history on page {page}."
            if not visible
            else f"History · page {page}:\n" + "\n\n".join(_format_job(job) for job in visible),
            buttons=buttons,
        )

    async def _cancel(self, event: Any, requester_id: int, text: str) -> None:
        parts = text.split()
        if len(parts) != 2:
            await event.respond("Usage: /cancel job-id")
            return
        try:
            resolved = await self.jobs.resolve(requester_id, parts[1])
            job = await self.jobs.cancel(requester_id, resolved.id)
        except JobNotFoundError:
            await event.respond("Job not found or ID is ambiguous.")
            return
        await event.respond(_format_job(job))

    async def _files(self, event: Any, actor_id: int, text: str) -> None:
        if self.library is None:
            await event.respond("Shared library is unavailable.")
            return
        page = _parse_page(text)
        if page is None:
            await event.respond("Usage: /files [page]")
            return
        await self._render_files(event, actor_id, page)

    async def _render_files(self, event: Any, actor_id: int, page: int) -> None:
        assert self.library is not None
        files = await self.library.files(actor_id, limit=11, offset=(page - 1) * 10)
        visible = files[:10]
        buttons = await self._page_buttons(actor_id, "files-page", page, has_next=len(files) > 10)
        await event.respond(
            f"No files on page {page}."
            if not visible
            else f"Shared files · page {page}:\n"
            + "\n".join(_format_stored_file(item) for item in visible),
            buttons=buttons,
        )

    async def _dirs(self, event: Any, actor_id: int, text: str) -> None:
        page = _parse_page(text)
        if self.library is None or page is None:
            await event.respond("Usage: /dirs [page]")
            return
        directories = await self.library.directories(actor_id)
        visible = directories[(page - 1) * 10 : page * 10]
        await event.respond(
            f"No archive directories on page {page}."
            if not visible
            else f"Archive directories · page {page}:\n"
            + "\n".join(f"• {item}" for item in visible)
        )

    async def _stats(self, event: Any, actor_id: int, text: str) -> None:
        if self.library is None or len(text.split()) != 1:
            await event.respond("Usage: /stats")
            return
        stats = await self.library.stats(actor_id)
        await event.respond(
            "Library statistics:\n"
            f"Files: {stats.file_count}\n"
            f"Size: {_human_size(stats.total_bytes)}\n"
            f"Inbox: {stats.inbox_count}\nArchive: {stats.archive_count}"
        )

    async def _rename(self, event: Any, actor_id: int, text: str) -> None:
        parts = text.split(maxsplit=2)
        if self.library is None or len(parts) != 3:
            await event.respond("Usage: /rename file-id new-name")
            return
        await self._organize_command(event, actor_id, parts[1], new_name=parts[2])

    async def _archive(self, event: Any, actor_id: int, text: str) -> None:
        parts = text.split(maxsplit=2)
        if self.library is None or len(parts) != 3:
            await event.respond("Usage: /archive file-id directory")
            return
        await self._organize_command(event, actor_id, parts[1], archive_directory=parts[2])

    async def _mkdir(self, event: Any, actor_id: int, text: str) -> None:
        parts = text.split(maxsplit=1)
        if self.library is None or len(parts) != 2:
            await event.respond("Usage: /mkdir directory")
            return
        try:
            created = await self.library.mkdir(actor_id, parts[1])
        except (OSError, ValueError) as error:
            await event.respond(f"Cannot create directory: {error}")
            return
        await event.respond(f"Created: {created}")

    async def _organize_command(
        self,
        event: Any,
        actor_id: int,
        identifier: str,
        *,
        new_name: str | None = None,
        archive_directory: str | None = None,
    ) -> None:
        assert self.library is not None
        try:
            stored = await self.library.organize(
                actor_id,
                identifier,
                new_name=new_name,
                archive_directory=archive_directory,
            )
        except (OSError, RuntimeError, StoredFileNotFoundError, ValueError) as error:
            await event.respond(f"Cannot organize file: {error}")
            return
        await event.respond(_format_stored_file(stored))

    async def _continue_interaction(self, event: Any, actor_id: int, text: str) -> bool:
        if self.interactions is None or self.library is None or not text:
            return False
        session = await self.interactions.latest_interaction_for_actor(actor_id)
        if session is None:
            return False
        if session.step == "await-name":
            next_step = "await-directory" if session.action == "rename-and-archive" else "confirm"
            updated = await self.interactions.update_interaction(
                session.id,
                actor_id,
                step=next_step,
                pending_name=text,
                pending_directory=session.pending_directory,
            )
            if next_step == "await-directory":
                await self._prompt_directories(event, actor_id, updated)
            else:
                await event.respond(_confirmation_text(updated), buttons=_confirm_buttons(updated))
            return True
        if session.step in {"await-directory", "await-new-directory"}:
            updated = await self.interactions.update_interaction(
                session.id,
                actor_id,
                step="confirm",
                pending_name=session.pending_name,
                pending_directory=text,
            )
            await event.respond(_confirmation_text(updated), buttons=_confirm_buttons(updated))
            return True
        return False

    async def _prompt_directories(
        self, event: Any, actor_id: int, session: InteractionSession
    ) -> None:
        assert self.library is not None
        directories = await self.library.directories(actor_id)
        buttons = [
            [
                Button.inline(
                    str(directory),
                    data=f"tg:{session.id}:directory:{index}".encode(),
                )
            ]
            for index, directory in enumerate(directories[:8])
        ]
        buttons.append(
            [Button.inline("New directory", data=f"tg:{session.id}:new-directory".encode())]
        )
        await event.respond(
            "Choose an archive directory, or send its path as text.",
            buttons=buttons,
        )

    async def _select_directory(
        self,
        event: Any,
        actor_id: int,
        session: InteractionSession,
        token: str,
    ) -> None:
        assert self.library is not None and self.interactions is not None
        try:
            index = int(token.split(":", 1)[1])
            directory = (await self.library.directories(actor_id))[index]
        except IndexError, ValueError:
            await _callback_reply(event, "That directory selection is stale.")
            return
        updated = await self.interactions.update_interaction(
            session.id,
            actor_id,
            step="confirm",
            pending_name=session.pending_name,
            pending_directory=directory.as_posix(),
        )
        await event.respond(_confirmation_text(updated), buttons=_confirm_buttons(updated))

    async def _page_buttons(
        self,
        actor_id: int,
        action: str,
        page: int,
        *,
        has_next: bool,
    ) -> list[list[Button]] | None:
        if self.interactions is None:
            return None
        targets = []
        if page > 1:
            targets.append(("Previous", page - 1))
        if has_next:
            targets.append(("Next", page + 1))
        if not targets:
            return None
        expires_at = datetime.now(UTC) + _INTERACTION_LIFETIME
        buttons = []
        for label, target in targets:
            session = await self.interactions.create_interaction(
                actor_id,
                action,
                str(target),
                expires_at=expires_at,
            )
            buttons.append(Button.inline(label, data=f"tg:{session.id}:{session.action}".encode()))
        return [buttons]

    async def _confirm_interaction(
        self, event: Any, actor_id: int, session: InteractionSession
    ) -> None:
        if session.step != "confirm" or self.library is None or self.interactions is None:
            await _callback_reply(event, "This confirmation is stale.")
            return
        directory = session.pending_directory
        try:
            if directory and directory.startswith("+"):
                directory = directory[1:].strip()
                await self.library.mkdir(actor_id, directory)
            stored = await self.library.organize(
                actor_id,
                session.job_id.hex if session.job_id else "",
                new_name=session.pending_name,
                archive_directory=directory,
            )
        except (OSError, RuntimeError, StoredFileNotFoundError, ValueError) as error:
            await _callback_reply(event, f"Cannot organize file: {error}")
            return
        assert session.job_id is not None
        await self.interactions.delete_interactions_for_job(session.job_id)
        await _callback_reply(event, "Completed:\n" + _format_stored_file(stored))


class TelegramJobNotifier:
    def __init__(self, bot_client: Any, interactions: InteractionRepository | None = None) -> None:
        self.bot_client = bot_client
        self.interactions = interactions

    async def job_finished(self, job: DownloadJob) -> None:
        if job.status is not JobStatus.COMPLETED or self.interactions is None:
            await self.bot_client.send_message(job.requester_id, _format_job(job))
            return
        expires_at = datetime.now(UTC) + _INTERACTION_LIFETIME
        sessions = []
        for action in ("keep", "rename", "archive", "rename-and-archive"):
            sessions.append(
                await self.interactions.create_interaction(
                    job.requester_id,
                    action,
                    "ready",
                    expires_at=expires_at,
                    job_id=job.id,
                )
            )
        buttons = [
            [
                _action_button("Keep", sessions[0]),
                _action_button("Rename", sessions[1]),
            ],
            [
                _action_button("Archive", sessions[2]),
                _action_button("Rename + archive", sessions[3]),
            ],
        ]
        await self.bot_client.send_message(
            job.requester_id,
            _format_job(job) + "\n\nChoose how to organize this file:",
            buttons=buttons,
        )


def _accepted(job: DownloadJob) -> str:
    return f"Job accepted · {job.short_id}\nStatus: {job.status.value}"


def _format_job(job: DownloadJob) -> str:
    title = job.display_title or job.original_file_name or job.reserved_name or "Untitled media"
    lines = [f"[{job.short_id}] {title}", f"Status: {job.status.value}"]
    if job.original_file_name:
        lines.append(f"File: {job.original_file_name}")
    if job.total_bytes is not None:
        lines.append(f"Size: {_human_size(job.total_bytes)}")
    if job.status is JobStatus.RUNNING and job.total_bytes:
        percentage = min(100, int(job.progress_bytes * 100 / job.total_bytes))
        lines.append(f"Progress: {percentage}% · {_human_size(job.progress_bytes)}")
    elif not job.is_terminal:
        lines.append(f"Progress: {_human_size(job.progress_bytes)}")
    if job.status is JobStatus.FAILED and job.error:
        lines.append(f"Error: {job.error}")
    return "\n".join(lines)


def _format_stored_file(stored: StoredFile) -> str:
    title = stored.display_title or stored.current_name
    return f"[{stored.short_id}] {title} · {_human_size(stored.size_bytes)}\n{stored.relative_path}"


def _human_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def _parse_page(text: str) -> int | None:
    parts = text.split()
    if len(parts) == 1:
        return 1
    if len(parts) != 2:
        return None
    try:
        page = int(parts[1])
    except ValueError:
        return None
    return page if page > 0 else None


def _parse_callback(data: bytes | str) -> tuple[UUID, str] | None:
    try:
        value = data.decode("ascii") if isinstance(data, bytes) else data
        prefix, identifier, token = value.split(":", 2)
        if prefix != "tg":
            return None
        return UUID(identifier), token
    except UnicodeError, ValueError:
        return None


def _valid_callback_token(session: InteractionSession, token: str) -> bool:
    if token in {session.action, "confirm", "cancel", "new-directory"}:
        return True
    if not token.startswith("directory:"):
        return False
    try:
        return int(token.split(":", 1)[1]) >= 0
    except ValueError:
        return False


def _action_button(label: str, session: InteractionSession) -> Button:
    return Button.inline(label, data=f"tg:{session.id}:{session.action}".encode())


def _confirm_buttons(session: InteractionSession) -> list[list[Button]]:
    return [
        [
            Button.inline("Confirm", data=f"tg:{session.id}:confirm".encode()),
            Button.inline("Cancel", data=f"tg:{session.id}:cancel".encode()),
        ]
    ]


def _confirmation_text(session: InteractionSession) -> str:
    lines = ["Confirm file organization:"]
    if session.pending_name:
        lines.append(f"Name: {session.pending_name}")
    if session.pending_directory:
        lines.append(f"Directory: {session.pending_directory}")
    return "\n".join(lines)


async def _callback_reply(event: Any, text: str) -> None:
    answer = getattr(event, "answer", None)
    if answer is not None:
        await answer(text)
        return
    await event.respond(text)
