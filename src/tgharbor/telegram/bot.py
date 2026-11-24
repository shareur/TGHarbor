from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol
from uuid import UUID, uuid4

from telethon import Button, events
from telethon.errors import (
    ChatWriteForbiddenError,
    MessageAuthorRequiredError,
    MessageEditTimeExpiredError,
    MessageIdInvalidError,
    MessageNotModifiedError,
)
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault, BotCommandScopeUsers

from tgharbor import __version__
from tgharbor.core.job_api import JobAPI, JobNotFoundError, UnauthorizedUserError
from tgharbor.core.jobs import (
    BatchNotice,
    BatchProgress,
    DownloadJob,
    FileLocation,
    InteractionAction,
    InteractionSession,
    InteractionStep,
    JobStatus,
    SourceSession,
    StoredFile,
)
from tgharbor.core.library_api import (
    BatchMoveResult,
    BatchNotFoundError,
    LibraryAPI,
    StoredFileNotFoundError,
)
from tgharbor.storage import validate_relative_directory
from tgharbor.telegram.links import MessageReferenceError, parse_message_link

_HELP = (
    "Send or forward media, or send one https://t.me/... message link.\n"
    "Albums sent to the Bot are accepted as one batch.\n"
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
# Public so startup recovery in the assembly layer can renew the same actionable
# lifetime that interactive cards use.
INTERACTION_LIFETIME = timedelta(hours=24)
_INTERACTION_LIFETIME = INTERACTION_LIFETIME
_TELEGRAM_TEXT_LIMIT = 4096
_ALBUM_BATCH_VERSION = "tgharbor-album-v1"
_DIRECTORY_TOKEN_VERSION = "tgharbor-directory-v1"
_DIRECTORY_DIGEST_LENGTH = 12
_DIRECTORY_PAGE_SIZE = 8
_TELEGRAM_CALLBACK_LIMIT = 64
_CONFIRM_STEPS = frozenset({InteractionStep.CONFIRM, InteractionStep.CONFIRM_NEW_DIRECTORY})
_WORKING_STEPS = frozenset(
    {
        InteractionStep.WORKING,
        InteractionStep.WORKING_CONFIRM,
        InteractionStep.WORKING_CONFIRM_NEW_DIRECTORY,
    }
)
# Pending-delivery rows own an undelivered terminal or retryable result. They are
# deliberately non-actionable: callbacks on them are stale until delivery settles
# the row into ready (retry) or removes it (terminal result).
_PENDING_STEPS = frozenset(
    {
        InteractionStep.RESULT_PENDING,
        InteractionStep.RETRY_PENDING,
    }
)
_CANCELLABLE_STEPS = frozenset(
    {
        InteractionStep.READY,
        InteractionStep.AWAIT_NAME,
        InteractionStep.AWAIT_DIRECTORY,
        InteractionStep.AWAIT_NEW_DIRECTORY,
        InteractionStep.CONFIRM,
        InteractionStep.CONFIRM_NEW_DIRECTORY,
    }
)
_DIRECTORY_ACTIONS = frozenset(
    {
        InteractionAction.ARCHIVE,
        InteractionAction.RENAME_AND_ARCHIVE,
        InteractionAction.BATCH_MOVE,
    }
)
_ACTION_TOKENS = frozenset(
    {
        InteractionAction.KEEP,
        InteractionAction.RENAME,
        InteractionAction.ARCHIVE,
        InteractionAction.RENAME_AND_ARCHIVE,
        InteractionAction.BATCH_MOVE,
        InteractionAction.INTERRUPTED_VIEW,
        InteractionAction.INTERRUPTED_CONTINUE,
    }
)
_PAGE_ACTIONS = frozenset({InteractionAction.FILES_PAGE, InteractionAction.HISTORY_PAGE})
_UNAUTHORIZED = "Unauthorized."
_STALE_ACTION = "This action is stale or invalid."
_STALE_ANCHOR = "This action belongs to an older card. Use the latest prompt."
_ALREADY_PROCESSING = "This action is already being processed."
_SAFE_DELIVERY_FAILURE = "This action could not be delivered. Please try again."
_STALE_FILE = "The target file is no longer available."
_INVALID_INPUT = "The name or directory is invalid. Review the values and try again."
_ORGANIZE_FAILURE = (
    "The file could not be organized because the library state changed. Refresh and try again."
)
_DIRECTORY_CREATE_FAILURE = (
    "The archive directory could not be created. Choose a different name or path."
)
_BATCH_MOVE_FAILURE = (
    "The batch could not be moved because the library state changed. Refresh and try again."
)
_STALE_DIRECTORY = (
    "That directory selection is stale because the directory list changed. "
    "Choose a directory from the refreshed list."
)
_BATCH_RETRY_NOTICE = "Some files were not moved. Choose a directory again to retry."
_INTERRUPTED_ACTION = "The previous action was interrupted. Choose how to continue."
_PAGE_EXPIRED_NOTICE = "This page action expired. Use /files or /history again."
_PAGE_RETRY_NOTICE = "The page could not be updated. Retry to open the requested page."
_MOVE_ALL_LABEL = "Move all to new folder"
_RETRY_MOVE_LABEL = "Retry move"
_RETRY_PAGE_LABEL = "Retry"
_FOLDER_PROMPT = "Send a relative path, for example: videos/2026. Do not start with /."
_FOLDER_INVALID = (
    "That is not a valid relative directory. "
    "Send a relative path, for example: videos/2026. Do not start with /."
)
_WORKING_FILE = "Working… Moving the file."
_WORKING_BATCH = "Working… Moving all files."
_KEPT_NOTICE = "Kept in Inbox. The file remains in Inbox."
_CONFIRM_EXISTING_LABEL = "Move here"
_CONFIRM_NEW_LABEL = "Create and move"
_BACK_LABEL = "Back"
_CANCEL_LABEL = "Cancel"
_PAGE_ACTION_TOKENS = frozenset({"files-page", "history-page"})
_READY_ACTION_LABELS = {
    InteractionAction.KEEP: "Keep",
    InteractionAction.RENAME: "Rename",
    InteractionAction.ARCHIVE: "Choose folder",
    InteractionAction.RENAME_AND_ARCHIVE: "Rename + archive",
}
_BOT_COMMANDS = (
    ("start", "Show help and usage"),
    ("files", "List shared files"),
    ("status", "Show active jobs or one job"),
    ("history", "Show completed, failed, or cancelled jobs"),
    ("dirs", "List archive directories"),
    ("cancel", "Cancel a job or the active prompt"),
    ("stats", "Show library statistics"),
    ("rename", "Rename a stored file"),
    ("archive", "Move a file into an archive directory"),
    ("mkdir", "Create an archive directory"),
    ("help", "Show help and usage"),
)

logger = logging.getLogger(__name__)

# Edit failures that establish the originating message cannot be changed. Only
# these errors justify one replacement send; timeouts, flood waits, oversized
# messages, ambiguous RPC failures, and arbitrary exceptions must not duplicate.
_EDIT_FALLBACK_ERRORS = (
    ChatWriteForbiddenError,
    MessageAuthorRequiredError,
    MessageEditTimeExpiredError,
    MessageIdInvalidError,
)


class _DeliveryFailure(RuntimeError):
    """A user-visible delivery failed without a safe replacement send."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


@dataclass(frozen=True, slots=True)
class _PageView:
    """Rendered page content plus the interactions bound to its buttons."""

    text: str
    buttons: list[list[Button]] | None
    sessions: tuple[InteractionSession, ...]


class InteractionRepository(Protocol):
    async def create_interaction(
        self,
        actor_id: int,
        action: str,
        step: str,
        *,
        expires_at: datetime,
        job_id: UUID | None = None,
        batch_id: str | None = None,
        prompt_message_id: int | None = None,
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
        prompt_message_id: int | None = None,
    ) -> InteractionSession: ...

    async def compare_and_set_prompt_message_id(
        self,
        interaction_id: UUID,
        actor_id: int,
        *,
        expected_prompt_message_id: int | None,
        prompt_message_id: int,
    ) -> InteractionSession | None: ...

    async def activate_text_workflow(
        self,
        interaction_id: UUID,
        actor_id: int,
        *,
        action: str,
        expected_steps: Sequence[str],
        step: str,
        pending_name: str | None = None,
        pending_directory: str | None = None,
        prompt_message_id: int | None = None,
    ) -> InteractionSession | None: ...

    async def claim_working_interaction(
        self,
        interaction_id: UUID,
        actor_id: int,
        *,
        action: str,
        expected_steps: Sequence[str],
    ) -> InteractionSession | None: ...

    async def restore_working_interaction(
        self,
        interaction_id: UUID,
        actor_id: int,
        *,
        step: str,
        prompt_message_id: int | None,
        expires_at: datetime,
        pending_name: str | None = None,
        pending_directory: str | None = None,
    ) -> InteractionSession | None: ...

    async def persist_result_pending(
        self,
        interaction_id: UUID,
        actor_id: int,
        *,
        action: str,
        expected_steps: Sequence[str],
        job_id: UUID | None = None,
    ) -> InteractionSession | None: ...

    async def persist_retry_pending(
        self,
        interaction_id: UUID,
        actor_id: int,
        *,
        action: str,
        expected_steps: Sequence[str],
    ) -> InteractionSession | None: ...

    async def bind_pending_prompt_message_id(
        self,
        interaction_id: UUID,
        actor_id: int,
        *,
        expected_step: str,
        expected_prompt_message_id: int | None,
        prompt_message_id: int,
    ) -> InteractionSession | None: ...

    async def finish_result_delivery(
        self,
        interaction_id: UUID,
        actor_id: int,
        *,
        action: str,
        expected_step: str,
        expected_prompt_message_id: int | None,
    ) -> bool: ...

    async def mark_retry_ready(
        self,
        interaction_id: UUID,
        actor_id: int,
        *,
        action: str,
        expected_prompt_message_id: int | None,
        expires_at: datetime,
    ) -> InteractionSession | None: ...

    async def consume_page_interaction(
        self,
        interaction_id: UUID,
        actor_id: int,
        *,
        action: str,
        step: str,
    ) -> InteractionSession | None: ...

    async def restore_page_interaction(
        self,
        session: InteractionSession,
        *,
        now: float | None = None,
    ) -> bool: ...

    async def delete_interaction(self, interaction_id: UUID, actor_id: int) -> bool: ...

    async def delete_interactions_for_job(self, job_id: UUID) -> int: ...


class NotificationRepository(InteractionRepository, Protocol):
    async def get_file(self, job_id: UUID) -> StoredFile | None: ...

    async def batch_progress_for_requester(
        self, requester_id: int, batch_id: str
    ) -> BatchProgress | None: ...

    async def claim_batch_notification(
        self,
        batch_id: str,
        requester_id: int,
        *,
        claimed_by: str,
        now: float | None = None,
        stale_after_seconds: float = 300,
    ) -> bool: ...

    async def mark_batch_notification_sent(
        self,
        batch_id: str,
        requester_id: int,
        *,
        claimed_by: str,
        now: float | None = None,
    ) -> bool: ...

    async def release_batch_notification(
        self,
        batch_id: str,
        requester_id: int,
        *,
        claimed_by: str,
        now: float | None = None,
    ) -> bool: ...

    async def list_claimable_batch_notifications(
        self,
        *,
        now: float | None = None,
        stale_after_seconds: float = 300,
        limit: int = 20,
    ) -> list[BatchNotice]: ...

    async def ensure_batch_move_interaction(
        self,
        requester_id: int,
        batch_id: str,
        *,
        expires_at: datetime,
        now: float | None = None,
    ) -> InteractionSession: ...

    async def active_batch_move_interaction(
        self, requester_id: int, batch_id: str
    ) -> InteractionSession | None: ...


class BatchNoticeSender(Protocol):
    repository: NotificationRepository | None

    async def deliver_batch_notice(
        self,
        batch_id: str,
        requester_id: int,
        *,
        stale_after_seconds: float | None = None,
    ) -> bool: ...


class TelegramBotController:
    def __init__(
        self,
        client: Any,
        jobs: JobAPI,
        library: LibraryAPI | None = None,
        interactions: InteractionRepository | None = None,
        *,
        batch_identity: str = "telegram-bot",
    ) -> None:
        self.client = client
        self.jobs = jobs
        self.library = library
        self.interactions = interactions
        self.batch_identity = batch_identity
        self._prompt_editor = _PromptMessageEditor(client)
        self._message_handler = events.NewMessage(incoming=True)
        self._album_handler = events.Album()
        self._callback_handler = events.CallbackQuery()

    def start(self) -> None:
        self.client.add_event_handler(self.handle_event, self._message_handler)
        self.client.add_event_handler(self.handle_album, self._album_handler)
        if self.interactions is not None:
            self.client.add_event_handler(self.handle_callback, self._callback_handler)

    def stop(self) -> None:
        self.client.remove_event_handler(self.handle_event, self._message_handler)
        self.client.remove_event_handler(self.handle_album, self._album_handler)
        if self.interactions is not None:
            self.client.remove_event_handler(self.handle_callback, self._callback_handler)

    async def handle_event(self, event: Any) -> None:
        if not getattr(event, "is_private", True):
            return
        message = getattr(event, "message", None)
        if getattr(message, "grouped_id", None) is not None:
            # The album handler accepts the group exactly once.
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
        if command == "/cancel" and len(text.split()) == 1:
            if await self._cancel_active_text_workflow(event, requester_id):
                return
            await event.respond("Usage: /cancel job-id")
            return
        if command in handlers:
            await handlers[command](event, requester_id, text)
            return
        # Any other text, including unknown slash commands, belongs to an active
        # text workflow before it can ever be interpreted as a download request.
        if await self._continue_interaction(event, requester_id, text):
            return
        await self._submit(event, requester_id, text)

    async def handle_album(self, event: Any) -> None:
        if not getattr(event, "is_private", True):
            return
        requester_id = event.sender_id
        if requester_id is None or not await self._authorize(event, requester_id):
            return
        messages = [
            message
            for message in getattr(event, "messages", ())
            if getattr(message, "file", None) is not None
        ]
        if not messages:
            return
        grouped_id = getattr(event, "grouped_id", None) or getattr(messages[0], "grouped_id", None)
        if grouped_id is None:
            return
        chat_id = getattr(event, "chat_id", None)
        if chat_id is None:
            chat_id = getattr(messages[0], "chat_id", requester_id)
        source_chat = str(chat_id)
        batch_id = _album_batch_identifier(
            self.batch_identity,
            requester_id,
            source_chat,
            grouped_id,
        )
        origin_group_id, channel_id = _album_source_metadata(messages)
        jobs = []
        for message in messages:
            jobs.append(
                await self.jobs.submit(
                    requester_id,
                    SourceSession.BOT,
                    source_chat,
                    int(message.id),
                    batch_id=batch_id,
                    batch_expected_size=len(messages),
                    origin_group_id=origin_group_id,
                    channel_id=channel_id,
                )
            )
        if jobs:
            await event.respond(
                f"Album queued · {len(messages)} files\n"
                f"Status: {jobs[0].status.value}\n"
                "Move all appears after all files complete."
            )

    async def handle_callback(self, event: Any) -> None:
        started = perf_counter()
        feedback = _CallbackFeedback(event, self.client)
        try:
            await feedback.acknowledge()
            await self._dispatch_callback(event, feedback)
        except asyncio.CancelledError:
            raise
        except _DeliveryFailure as error:
            logger.warning(
                "Telegram callback delivery failed: outcome=delivery-failed "
                "category=%s latency_ms=%.1f",
                error.category,
                (perf_counter() - started) * 1000,
            )
            try:
                await feedback.respond(_SAFE_DELIVERY_FAILURE)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - failure notice delivery is best effort
                logger.warning("Telegram callback delivery notice was not sent")
        except Exception as error:  # noqa: BLE001 - top-level callback boundary
            logger.error(
                "Telegram callback failed: outcome=failed error_type=%s latency_ms=%.1f",
                type(error).__name__,
                (perf_counter() - started) * 1000,
            )
            try:
                await feedback.respond(
                    "This action failed unexpectedly. Please try again or resend the command."
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - failure notice delivery is best effort
                logger.error("Telegram callback failure notice could not be delivered")

    async def _dispatch_callback(self, event: Any, feedback: _CallbackFeedback) -> None:
        actor_id = event.sender_id
        if actor_id is None:
            await feedback.deliver(_UNAUTHORIZED)
            return
        try:
            self.jobs.require_authorized(actor_id)
        except UnauthorizedUserError:
            await feedback.deliver(_UNAUTHORIZED)
            return
        parsed = _parse_callback(getattr(event, "data", b""))
        if parsed is None or self.interactions is None or self.library is None:
            await feedback.respond("This action is invalid or unavailable.")
            return
        interaction_id, token = parsed
        session = await self.interactions.get_interaction(interaction_id)
        if session is None:
            # A consumed token must never replace a newer card or its controls.
            await feedback.respond("This action is unauthorized, expired, or stale.")
            return
        if session.actor_id != actor_id or session.expires_at <= datetime.now(UTC):
            await feedback.deliver("This action is unauthorized, expired, or stale.")
            return
        if not _callback_token_matches(session, token):
            # Keep a live card, including any retry controls, intact on replay.
            await feedback.respond(_STALE_ACTION)
            return
        if not _callback_anchor_matches(event, session):
            # A superseded card must never mutate the live workflow or its anchor.
            await feedback.respond(_STALE_ANCHOR)
            return
        if session.step in _PENDING_STEPS:
            # Result-delivery outbox rows are not actionable until they settle.
            await feedback.respond(_STALE_ACTION)
            return
        if token == "cancel":
            header = await self._context_header(actor_id, session)
            await self.interactions.delete_interaction(session.id, actor_id)
            await feedback.deliver(f"{header}\n\nAction cancelled.")
            return
        if session.action == InteractionAction.INTERRUPTED_VIEW:
            await self._render_interrupted(actor_id, feedback)
            return
        if session.action == InteractionAction.INTERRUPTED_CONTINUE:
            await self._continue_interrupted(actor_id, feedback)
            return
        if session.action == InteractionAction.BATCH_MOVE:
            await self._handle_batch_callback(actor_id, session, token, feedback)
            return
        if token == "new-directory":
            updated = await self.interactions.activate_text_workflow(
                session.id,
                actor_id,
                action=session.action,
                expected_steps=(InteractionStep.READY, InteractionStep.AWAIT_DIRECTORY),
                step=InteractionStep.AWAIT_NEW_DIRECTORY,
                pending_name=session.pending_name,
                pending_directory=None,
            )
            if updated is None:
                await feedback.respond(_STALE_ACTION)
                return
            header = await self._context_header(actor_id, updated)
            await self._deliver_anchored_card(
                actor_id,
                updated,
                feedback,
                _folder_prompt_text(header),
                buttons=[[_cancel_button(updated)]],
            )
            return
        if token == "back":
            updated = await self.interactions.activate_text_workflow(
                session.id,
                actor_id,
                action=session.action,
                expected_steps=_CONFIRM_STEPS,
                step=InteractionStep.AWAIT_DIRECTORY,
                pending_name=session.pending_name,
                pending_directory=None,
            )
            if updated is None:
                await feedback.respond(_STALE_ACTION)
                return
            await self._respond_directories(actor_id, updated, feedback)
            return
        if token.startswith("directory:"):
            await self._select_directory(actor_id, session, token, feedback)
            return
        if session.action in _PAGE_ACTIONS:
            page = _positive_int(session.step)
            if page is None:
                await feedback.respond("This page action is stale.")
                return
            # The first atomic consumer owns the page token. Every concurrent
            # callback observes a missing row before it can create successors or
            # edit the card.
            consumed = await self.interactions.consume_page_interaction(
                session.id,
                actor_id,
                action=session.action,
                step=session.step,
            )
            if consumed is None:
                await feedback.respond(_STALE_ACTION)
                return
            origin = feedback.message_id
            successors: tuple[InteractionSession, ...] = ()
            try:
                view = await self._page_view(
                    session.action,
                    actor_id,
                    page,
                    prompt_message_id=origin,
                )
                successors = view.sessions
                if len(view.text) > _TELEGRAM_TEXT_LIMIT:
                    raise _DeliveryFailure("too-long")
            except asyncio.CancelledError:
                await self._rollback_page_transition(consumed, successors)
                raise
            except BaseException:
                await self._rollback_page_transition(consumed, successors)
                raise
            await self._deliver_page_successors(
                actor_id,
                consumed,
                successors,
                feedback,
                text=view.text,
                buttons=view.buttons,
                origin=origin,
            )
            return
        if session.job_id is None:
            await feedback.deliver(_STALE_FILE)
            return
        try:
            await self.library.resolve(actor_id, session.job_id.hex)
        except StoredFileNotFoundError:
            await self.interactions.delete_interactions_for_job(session.job_id)
            await feedback.deliver(_STALE_FILE)
            return
        if token == "confirm":
            await self._confirm_interaction(actor_id, session, feedback)
            return
        if session.action == InteractionAction.KEEP:
            await self._keep_interaction(actor_id, session, feedback)
            return
        step = (
            InteractionStep.AWAIT_NAME
            if session.action in {InteractionAction.RENAME, InteractionAction.RENAME_AND_ARCHIVE}
            else InteractionStep.AWAIT_DIRECTORY
        )
        updated = await self.interactions.activate_text_workflow(
            session.id,
            actor_id,
            action=session.action,
            expected_steps=(InteractionStep.READY,),
            step=step,
            pending_name=session.pending_name,
            pending_directory=session.pending_directory,
        )
        if updated is None:
            await feedback.respond(_STALE_ACTION)
            return
        header = await self._context_header(actor_id, updated)
        if step == InteractionStep.AWAIT_NAME:
            await self._deliver_anchored_card(
                actor_id,
                updated,
                feedback,
                f"{header}\n\nSend the new filename.",
                buttons=[[_cancel_button(updated)]],
            )
        else:
            await self._respond_directories(actor_id, updated, feedback)

    async def _authorize(self, event: Any, actor_id: int) -> bool:
        try:
            self.jobs.require_authorized(actor_id)
        except UnauthorizedUserError:
            await event.respond(_UNAUTHORIZED)
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
        view = await self._history_page(requester_id, page, prompt_message_id=None)
        message = await event.respond(view.text, buttons=view.buttons)
        await self._bind_page_anchor(requester_id, view.sessions, _message_identifier(message))

    async def _history_page(
        self,
        requester_id: int,
        page: int,
        *,
        prompt_message_id: int | None,
    ) -> _PageView:
        jobs = await self.jobs.history(requester_id, limit=11, offset=(page - 1) * 10)
        visible = jobs[:10]
        buttons, sessions = await self._page_buttons(
            requester_id,
            "history-page",
            page,
            has_next=len(jobs) > 10,
            prompt_message_id=prompt_message_id,
        )
        text = (
            f"No history on page {page}."
            if not visible
            else f"History · page {page}:\n" + "\n\n".join(_format_job(job) for job in visible)
        )
        return _PageView(text, buttons, sessions)

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
        view = await self._files_page(actor_id, page, prompt_message_id=None)
        message = await event.respond(view.text, buttons=view.buttons)
        await self._bind_page_anchor(actor_id, view.sessions, _message_identifier(message))

    async def _files_page(
        self,
        actor_id: int,
        page: int,
        *,
        prompt_message_id: int | None,
    ) -> _PageView:
        assert self.library is not None
        files = await self.library.files(actor_id, limit=11, offset=(page - 1) * 10)
        visible = files[:10]
        buttons, sessions = await self._page_buttons(
            actor_id,
            "files-page",
            page,
            has_next=len(files) > 10,
            prompt_message_id=prompt_message_id,
        )
        text = (
            f"No files on page {page}."
            if not visible
            else f"Shared files · page {page}:\n"
            + "\n".join(_format_stored_file(item) for item in visible)
        )
        return _PageView(text, buttons, sessions)

    async def _page_view(
        self,
        action: str,
        actor_id: int,
        page: int,
        *,
        prompt_message_id: int | None,
    ) -> _PageView:
        if action == InteractionAction.FILES_PAGE:
            return await self._files_page(actor_id, page, prompt_message_id=prompt_message_id)
        return await self._history_page(actor_id, page, prompt_message_id=prompt_message_id)

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
        except (OSError, RuntimeError, ValueError) as error:
            _log_library_failure("mkdir", error)
            await event.respond(_DIRECTORY_CREATE_FAILURE)
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
        except StoredFileNotFoundError as error:
            _log_library_failure("organize", error)
            await event.respond(_STALE_FILE)
            return
        except ValueError as error:
            _log_library_failure("organize", error)
            await event.respond(_INVALID_INPUT)
            return
        except (OSError, RuntimeError) as error:
            _log_library_failure("organize", error)
            await event.respond(_ORGANIZE_FAILURE)
            return
        await event.respond(_format_stored_file(stored))

    async def _continue_interaction(self, event: Any, actor_id: int, text: str) -> bool:
        if self.interactions is None or self.library is None or not text:
            return False
        session = await self.interactions.latest_interaction_for_actor(actor_id)
        if session is None:
            return False
        header = await self._context_header(actor_id, session)
        if session.step == InteractionStep.AWAIT_NAME:
            next_step = (
                InteractionStep.AWAIT_DIRECTORY
                if session.action == InteractionAction.RENAME_AND_ARCHIVE
                else InteractionStep.CONFIRM
            )
            updated = await self.interactions.activate_text_workflow(
                session.id,
                actor_id,
                action=session.action,
                expected_steps=(InteractionStep.AWAIT_NAME,),
                step=next_step,
                pending_name=text.strip(),
                pending_directory=session.pending_directory,
            )
            if updated is None:
                await event.respond(_STALE_ACTION)
                return True
            if next_step == InteractionStep.AWAIT_DIRECTORY:
                await self._prompt_directories(event, actor_id, updated)
            else:
                confirmation = await self._context_header(actor_id, updated)
                await self._deliver_text_prompt(
                    event,
                    actor_id,
                    updated,
                    _confirmation_text(updated, confirmation),
                    buttons=_confirm_buttons(updated),
                )
            return True
        if session.step in {InteractionStep.AWAIT_DIRECTORY, InteractionStep.AWAIT_NEW_DIRECTORY}:
            try:
                directory = validate_relative_directory(text)
            except ValueError, RuntimeError:
                # Invalid folder input stays in the same step and never creates a job.
                await self._prompt_directories(event, actor_id, session, error=_FOLDER_INVALID)
                return True
            next_step = (
                InteractionStep.CONFIRM_NEW_DIRECTORY
                if session.step == InteractionStep.AWAIT_NEW_DIRECTORY
                else InteractionStep.CONFIRM
            )
            updated = await self.interactions.activate_text_workflow(
                session.id,
                actor_id,
                action=session.action,
                expected_steps=(session.step,),
                step=next_step,
                pending_name=session.pending_name,
                pending_directory=directory.as_posix(),
            )
            if updated is None:
                await event.respond(_STALE_ACTION)
                return True
            confirmation = await self._context_header(actor_id, updated)
            await self._deliver_text_prompt(
                event,
                actor_id,
                updated,
                _confirmation_text(updated, confirmation),
                buttons=_confirm_buttons(updated),
            )
            return True
        if session.step in _CONFIRM_STEPS:
            await self._deliver_text_prompt(
                event,
                actor_id,
                session,
                _confirmation_text(session, header),
                buttons=_confirm_buttons(session),
            )
            return True
        return False

    async def _context_header(self, actor_id: int, session: InteractionSession) -> str:
        """Return a concise, privacy-safe header that survives every step."""

        if session.batch_id:
            try:
                progress = await self.jobs.batch_progress(actor_id, session.batch_id)
            except JobNotFoundError:
                return "Album · batch"
            total = progress.expected_size or progress.total_jobs
            return f"Album · {total} files"
        if session.job_id is None:
            return "Shared library"
        short = session.job_id.hex[:8]
        if self.library is not None:
            try:
                stored = await self.library.resolve(actor_id, session.job_id.hex)
            except StoredFileNotFoundError:
                return f"Job {short}"
            return f"Job {short} · {stored.display_title or stored.current_name}"
        return f"Job {short}"

    async def _cancel_active_text_workflow(self, event: Any, actor_id: int) -> bool:
        if self.interactions is None:
            return False
        session = await self.interactions.latest_interaction_for_actor(actor_id)
        if session is None:
            return False
        header = await self._context_header(actor_id, session)
        await self.interactions.delete_interaction(session.id, actor_id)
        try:
            await self._prompt_editor.edit(
                actor_id,
                session.prompt_message_id,
                f"{header}\n\nAction cancelled.",
            )
        except _DeliveryFailure:
            await event.respond(_SAFE_DELIVERY_FAILURE)
        return True

    async def _prompt_directories(
        self,
        event: Any,
        actor_id: int,
        session: InteractionSession,
        *,
        error: str | None = None,
    ) -> None:
        header = await self._context_header(actor_id, session)
        if session.step == InteractionStep.AWAIT_NEW_DIRECTORY:
            await self._deliver_text_prompt(
                event,
                actor_id,
                session,
                _folder_prompt_text(header, error=error),
                buttons=[[_cancel_button(session)]],
            )
            return
        assert self.library is not None
        directories = await self.library.directories(actor_id)
        text, buttons = _directory_choice_prompt(session, directories, header, error=error)
        await self._deliver_text_prompt(event, actor_id, session, text, buttons=buttons)

    async def _deliver_text_prompt(
        self,
        event: Any,
        actor_id: int,
        session: InteractionSession,
        text: str,
        *,
        buttons: Any | None = None,
    ) -> None:
        """Render a text-input prompt in its stored bot message when one exists."""

        try:
            if session.prompt_message_id is None:
                message = await event.respond(text, buttons=buttons)
                await self._bind_prompt(
                    session,
                    actor_id,
                    _message_identifier(message),
                    expected=None,
                )
                return
            new_message_id = await self._prompt_editor.edit(
                actor_id,
                session.prompt_message_id,
                text,
                buttons=buttons,
            )
            if new_message_id is not None:
                await self._bind_prompt(
                    session,
                    actor_id,
                    new_message_id,
                    expected=session.prompt_message_id,
                )
        except _DeliveryFailure:
            await event.respond(_SAFE_DELIVERY_FAILURE)

    async def _bind_prompt(
        self,
        session: InteractionSession,
        actor_id: int,
        message_id: int | None,
        *,
        expected: int | None,
    ) -> None:
        if message_id is None or self.interactions is None:
            return
        updated = await self.interactions.compare_and_set_prompt_message_id(
            session.id,
            actor_id,
            expected_prompt_message_id=expected,
            prompt_message_id=message_id,
        )
        if updated is None:
            logger.warning("Telegram prompt anchor update lost: outcome=stale-anchor")
            await self._disable_stale_message(actor_id, message_id)

    async def _disable_stale_message(self, actor_id: int, message_id: int) -> None:
        edit_message = getattr(self.client, "edit_message", None)
        if edit_message is None:
            return
        try:
            await edit_message(actor_id, message_id, "This prompt is stale.", buttons=None)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - disabling a lost card is best effort
            logger.warning(
                "Telegram stale prompt cleanup failed: outcome=cleanup-failed error_type=%s",
                type(error).__name__,
            )

    async def _bind_page_anchor(
        self,
        actor_id: int,
        sessions: tuple[InteractionSession, ...],
        message_id: int | None,
    ) -> None:
        if message_id is None:
            return
        for session in sessions:
            await self._bind_prompt(session, actor_id, message_id, expected=None)

    async def _deliver_anchored_card(
        self,
        actor_id: int,
        session: InteractionSession,
        feedback: _CallbackFeedback,
        text: str,
        *,
        buttons: Any | None = None,
    ) -> int | None:
        """Deliver a retained-control card and rebind its prompt anchor.

        Delivery is edit-first and returns the actual message identity, which is
        the original message, the unchanged edit, or a replacement send. The
        compare-and-swap therefore always moves the anchor from this session's
        expected old anchor to the message that now carries the live buttons.
        """

        expected = session.prompt_message_id
        message_id = await feedback.deliver(text, buttons=buttons)
        await self._bind_prompt(session, actor_id, message_id, expected=expected)
        return message_id

    async def _await_cancellation_safe(self, cleanup: Any) -> None:
        """Complete cleanup even when the surrounding task is cancelled."""

        task = asyncio.create_task(cleanup)
        await asyncio.shield(task)

    async def _run_critical(
        self,
        operation: Callable[[], Awaitable[Any]],
        cancellation: list[bool],
    ) -> Any:
        """Finish one critical delivery or rebind before honouring cancellation.

        The surrounding task may be cancelled while the operation runs. The
        operation itself keeps running under ``shield``; its result is returned
        so callers can leave the visible card consistent, and ``cancellation``
        records that the surrounding task must re-raise after commit.
        """

        task = asyncio.create_task(operation())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancellation[0] = True
            return await task

    async def _run_cancellation_safe(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        """Settle one critical operation, then propagate cancellation.

        The operation runs under a cancellation shield. An outer cancellation
        lets it finish and persist its state, then the cancellation is re-raised
        instead of being swallowed. A cancellation raised by the operation
        itself is propagated unchanged.
        """

        cancellation = [False]
        try:
            result = await self._run_critical(operation, cancellation)
        except asyncio.CancelledError:
            raise
        except Exception:
            if cancellation[0]:
                raise asyncio.CancelledError from None
            raise
        if cancellation[0]:
            raise asyncio.CancelledError
        return result

    async def _deliver_page_successors(
        self,
        actor_id: int,
        consumed: InteractionSession,
        successors: Sequence[InteractionSession],
        feedback: _CallbackFeedback,
        *,
        text: str,
        buttons: Any | None,
        origin: int | None,
    ) -> None:
        """Keep a visibly delivered page card consistent with persisted rows.

        Successors are created with the callback message as their anchor, so an
        edit on that same message is committed without a rebind. A replacement
        send moves every anchor in a shielded critical section. If that rebind
        fails, the replacement is disabled, the exact source interaction is
        restored, and the source card is re-rendered with its restored token.
        Rollback to the source happens only when no usable successor card is
        visible.
        """

        cancellation = [False]
        delivered_id: int | None = None
        try:
            delivered_id = await self._run_critical(
                lambda: feedback.deliver(text, buttons=buttons),
                cancellation,
            )
        except asyncio.CancelledError:
            # The delivery task was itself cancelled, so the remote outcome is
            # unknown. The successors already point at the callback message and
            # keeping them avoids deleting the tokens of a visible card.
            cancellation[0] = True
            delivered_id = origin
        except _DeliveryFailure:
            await self._rollback_page_transition(consumed, successors)
            if cancellation[0]:
                raise asyncio.CancelledError from None
            raise
        if delivered_id is None:
            delivered_id = origin
        if delivered_id is None:
            await self._rollback_page_transition(consumed, successors)
            raise _DeliveryFailure("send")
        if delivered_id != origin:
            bound = False
            rebind_cancellation = [False]
            try:
                bound = await self._run_critical(
                    lambda: self._bind_page_successors(
                        actor_id,
                        successors,
                        expected=origin,
                        message_id=delivered_id,
                    ),
                    rebind_cancellation,
                )
            except asyncio.CancelledError:
                bound = False
                rebind_cancellation[0] = True
            except Exception as error:  # noqa: BLE001 - visible recovery follows
                logger.warning(
                    "Telegram page anchor rebind failed: outcome=failed error_type=%s",
                    type(error).__name__,
                )
                bound = False
            cancellation[0] = cancellation[0] or rebind_cancellation[0]
            if not bound:
                cleanup_cancellation = [False]
                try:
                    await self._run_critical(
                        lambda: self._recover_page_rebind_failure(
                            actor_id,
                            consumed,
                            successors,
                            feedback,
                            origin=origin,
                            replacement_id=delivered_id,
                        ),
                        cleanup_cancellation,
                    )
                except asyncio.CancelledError:
                    cleanup_cancellation[0] = True
                except Exception as error:  # noqa: BLE001 - never abort recovery
                    logger.warning(
                        "Telegram page visible recovery failed: outcome=failed error_type=%s",
                        type(error).__name__,
                    )
                cancellation[0] = cancellation[0] or cleanup_cancellation[0]
        if cancellation[0]:
            raise asyncio.CancelledError

    async def _bind_page_successors(
        self,
        actor_id: int,
        successors: Sequence[InteractionSession],
        *,
        expected: int | None,
        message_id: int,
    ) -> bool:
        """Move every successor anchor from the old card to a replacement."""

        if self.interactions is None:
            return False
        bound = True
        for session in successors:
            updated = await self.interactions.compare_and_set_prompt_message_id(
                session.id,
                actor_id,
                expected_prompt_message_id=expected,
                prompt_message_id=message_id,
            )
            if updated is None:
                bound = False
        return bound

    async def _recover_page_rebind_failure(
        self,
        actor_id: int,
        consumed: InteractionSession,
        successors: Sequence[InteractionSession],
        feedback: _CallbackFeedback,
        *,
        origin: int | None,
        replacement_id: int | None,
    ) -> None:
        """Disable a replacement and make the restored source token visible.

        The replacement cannot remain a usable successor card once its rows are
        removed. The original interaction is restored first; then the original
        card is re-rendered (or one replacement is sent and bound) so the token
        that is live in the database is present in a visible card.
        """

        assert self.interactions is not None
        if replacement_id is not None and replacement_id != origin:
            await self._disable_stale_message(actor_id, replacement_id)
        await self._discard_sessions(successors)
        try:
            restored = await self.interactions.restore_page_interaction(consumed)
        except Exception as error:  # noqa: BLE001 - keep the original card safe
            logger.warning(
                "Telegram page rollback failed: outcome=failed error_type=%s",
                type(error).__name__,
            )
            restored = False
        if not restored:
            if origin is not None:
                await self._disable_stale_message(actor_id, origin)
            return
        text = feedback.original_text or _PAGE_RETRY_NOTICE
        buttons = [
            [
                Button.inline(
                    _RETRY_PAGE_LABEL,
                    data=f"tg:{consumed.id}:{consumed.action}".encode(),
                )
            ]
        ]
        try:
            new_id = await self._prompt_editor.edit(actor_id, origin, text, buttons=buttons)
        except _DeliveryFailure:
            try:
                new_id = await self._prompt_editor.edit(actor_id, None, text, buttons=buttons)
            except _DeliveryFailure:
                return
        if new_id is not None:
            try:
                await self._bind_prompt(consumed, actor_id, new_id, expected=origin)
            except Exception:
                # Never leave a visible retry card whose token was not bound.
                await self._disable_stale_message(actor_id, new_id)
                raise

    async def _discard_sessions(self, sessions: Sequence[InteractionSession]) -> None:
        """Best-effort removal used by rollback and cancellation paths."""

        if self.interactions is None:
            return
        for session in sessions:
            try:
                await self.interactions.delete_interaction(session.id, session.actor_id)
            except Exception as error:  # noqa: BLE001 - orphan cleanup is best effort
                logger.warning(
                    "Telegram page session cleanup failed: outcome=failed error_type=%s",
                    type(error).__name__,
                )

    async def _rollback_page_transition(
        self,
        consumed: InteractionSession,
        successors: Sequence[InteractionSession],
    ) -> None:
        async def cleanup() -> None:
            await self._discard_sessions(successors)
            if self.interactions is None:
                return
            try:
                restored = await self.interactions.restore_page_interaction(consumed)
            except Exception as error:  # noqa: BLE001 - keep the old page when possible
                logger.warning(
                    "Telegram page rollback failed: outcome=failed error_type=%s",
                    type(error).__name__,
                )
                return
            if not restored:
                logger.warning("Telegram page rollback skipped: outcome=expired")

        await self._await_cancellation_safe(cleanup())

    async def _delete_sessions(self, sessions: tuple[InteractionSession, ...]) -> None:
        if self.interactions is None:
            return
        for session in sessions:
            try:
                await self.interactions.delete_interaction(session.id, session.actor_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - orphan cleanup is best effort
                logger.warning(
                    "Telegram page session cleanup failed: outcome=failed error_type=%s",
                    type(error).__name__,
                )

    async def _respond_directories(
        self,
        actor_id: int,
        session: InteractionSession,
        feedback: _CallbackFeedback,
    ) -> None:
        header = await self._context_header(actor_id, session)
        assert self.library is not None
        directories = await self.library.directories(actor_id)
        text, buttons = _directory_choice_prompt(session, directories, header)
        await self._deliver_anchored_card(
            actor_id,
            session,
            feedback,
            text,
            buttons=buttons,
        )

    async def _select_directory(
        self,
        actor_id: int,
        session: InteractionSession,
        token: str,
        feedback: _CallbackFeedback,
    ) -> None:
        assert self.library is not None and self.interactions is not None
        directories = await self.library.directories(actor_id)
        header = await self._context_header(actor_id, session)
        directory = _match_directory_selection(token, directories)
        if directory is None:
            text, buttons = _directory_choice_prompt(
                session, directories, header, error=_STALE_DIRECTORY
            )
            await self._deliver_anchored_card(
                actor_id,
                session,
                feedback,
                text,
                buttons=buttons,
            )
            return
        updated = await self.interactions.activate_text_workflow(
            session.id,
            actor_id,
            action=session.action,
            expected_steps=(InteractionStep.AWAIT_DIRECTORY,),
            step=InteractionStep.CONFIRM,
            pending_name=session.pending_name,
            pending_directory=directory.as_posix(),
        )
        if updated is None:
            await feedback.respond(_STALE_ACTION)
            return
        await self._deliver_anchored_card(
            actor_id,
            updated,
            feedback,
            _confirmation_text(updated, header),
            buttons=_confirm_buttons(updated),
        )

    async def _handle_batch_callback(
        self,
        actor_id: int,
        session: InteractionSession,
        token: str,
        feedback: _CallbackFeedback,
    ) -> None:
        assert self.interactions is not None and self.library is not None
        if token == "back":
            updated = await self.interactions.activate_text_workflow(
                session.id,
                actor_id,
                action=session.action,
                expected_steps=_CONFIRM_STEPS,
                step=InteractionStep.AWAIT_DIRECTORY,
                pending_name=session.pending_name,
                pending_directory=None,
            )
            if updated is None:
                await feedback.respond(_STALE_ACTION)
                return
            await self._respond_directories(actor_id, updated, feedback)
            return
        if token == InteractionAction.BATCH_MOVE:
            updated = await self.interactions.activate_text_workflow(
                session.id,
                actor_id,
                action=session.action,
                expected_steps=(InteractionStep.READY,),
                step=InteractionStep.AWAIT_DIRECTORY,
                pending_name=session.pending_name,
                pending_directory=None,
            )
            if updated is None:
                await feedback.respond(_STALE_ACTION)
                return
            await self._respond_directories(actor_id, updated, feedback)
            return
        if token == "new-directory":
            updated = await self.interactions.activate_text_workflow(
                session.id,
                actor_id,
                action=session.action,
                expected_steps=(InteractionStep.READY, InteractionStep.AWAIT_DIRECTORY),
                step=InteractionStep.AWAIT_NEW_DIRECTORY,
                pending_name=session.pending_name,
                pending_directory=None,
            )
            if updated is None:
                await feedback.respond(_STALE_ACTION)
                return
            header = await self._context_header(actor_id, updated)
            await self._deliver_anchored_card(
                actor_id,
                updated,
                feedback,
                _folder_prompt_text(header),
                buttons=[[_cancel_button(updated)]],
            )
            return
        if token.startswith("directory:"):
            await self._select_directory(actor_id, session, token, feedback)
            return
        if token == "confirm":
            await self._confirm_batch_move(actor_id, session, feedback)
            return
        await feedback.respond(_STALE_ACTION)

    async def _confirm_batch_move(
        self,
        actor_id: int,
        session: InteractionSession,
        feedback: _CallbackFeedback,
    ) -> None:
        if (
            (session.step not in _CONFIRM_STEPS and session.step not in _WORKING_STEPS)
            or self.library is None
            or self.interactions is None
        ):
            await feedback.respond("This confirmation is stale.")
            return
        directory = session.pending_directory
        if not directory or not session.batch_id:
            await feedback.deliver(_INVALID_INPUT)
            return
        header = await self._context_header(actor_id, session)
        try:
            validate_relative_directory(directory)
        except ValueError, RuntimeError:
            await self._deliver_anchored_card(
                actor_id,
                session,
                feedback,
                f"{header}\n\n{_INVALID_INPUT}",
                buttons=_back_cancel_buttons(session),
            )
            return
        claimed = await self.interactions.claim_working_interaction(
            session.id,
            actor_id,
            action=session.action,
            expected_steps=_CONFIRM_STEPS,
        )
        if claimed is None:
            await self._report_claim_loss(actor_id, session, feedback)
            return
        prior_step = session.step
        prior_anchor = claimed.prompt_message_id
        prior_name = session.pending_name
        prior_directory = session.pending_directory
        handoff = False

        async def commit_and_handoff() -> tuple[InteractionSession | None, str | None]:
            """Create/organize and persist the committed batch result atomically.

            This whole body runs under a cancellation shield. Any per-file work,
            the decision between terminal and retryable state, and the pending
            row persistence therefore settle before an outer cancellation is
            re-raised, and no committed batch ever restores a pristine
            confirmation.
            """

            assert self.interactions is not None
            if prior_step == InteractionStep.CONFIRM_NEW_DIRECTORY:
                try:
                    await self.library.mkdir(actor_id, directory)
                except (OSError, RuntimeError, ValueError) as error:
                    _log_library_failure("mkdir", error)
                    restored = await self._restore_working(
                        actor_id,
                        claimed,
                        step=prior_step,
                        prompt_message_id=prior_anchor,
                        pending_name=prior_name,
                        pending_directory=prior_directory,
                    )
                    retained = restored or session
                    await self._deliver_anchored_card(
                        actor_id,
                        retained,
                        feedback,
                        f"{header}\n\n{_DIRECTORY_CREATE_FAILURE}",
                        buttons=_back_cancel_buttons(retained),
                    )
                    return None, None
            try:
                result = await self.library.organize_batch(
                    actor_id,
                    session.batch_id,
                    directory,
                )
            except BatchNotFoundError as error:
                _log_library_failure("organize_batch", error)
                await self.interactions.delete_interaction(session.id, actor_id)
                await feedback.deliver(f"{header}\n\nThis batch is no longer available.")
                return None, None
            except ValueError as error:
                _log_library_failure("organize_batch", error)
                restored = await self._restore_working(
                    actor_id,
                    claimed,
                    step=prior_step,
                    prompt_message_id=prior_anchor,
                    pending_name=prior_name,
                    pending_directory=prior_directory,
                )
                retained = restored or session
                await self._deliver_anchored_card(
                    actor_id,
                    retained,
                    feedback,
                    f"{header}\n\n{_INVALID_INPUT}",
                    buttons=_back_cancel_buttons(retained),
                )
                return None, None
            except (OSError, RuntimeError) as error:
                # Per-file work may already have committed, so persist the
                # retryable result before an outer cancellation can be re-raised.
                _log_library_failure("organize_batch", error)
                pending = await self._persist_batch_result(actor_id, claimed, retry=True)
                text = f"{header}\n\n{_BATCH_MOVE_FAILURE}"
            else:
                summary = _format_batch_result(result)
                if result.failed or result.not_ready:
                    pending = await self._persist_batch_result(actor_id, claimed, retry=True)
                    text = f"{header}\n\n{summary}\n\n{_BATCH_RETRY_NOTICE}"
                else:
                    pending = await self._persist_batch_result(actor_id, claimed, retry=False)
                    text = f"{header}\n\n{summary}"
            if pending is None:
                # The claim changed; deliver the result once without consuming
                # or overwriting another owner's row.
                await feedback.deliver(text)
                return None, None
            return pending, text

        try:
            try:
                working_message_id = await feedback.deliver(f"{header}\n\n{_WORKING_BATCH}")
            except _DeliveryFailure:
                await self._restore_working(
                    actor_id,
                    claimed,
                    step=prior_step,
                    prompt_message_id=prior_anchor,
                    pending_name=prior_name,
                    pending_directory=prior_directory,
                )
                raise
            await self._bind_prompt(
                claimed,
                actor_id,
                working_message_id,
                expected=prior_anchor,
            )
            handoff = True
            pending, text = await self._run_cancellation_safe(commit_and_handoff)
            if pending is None or text is None:
                return
            buttons = (
                _retry_cancel_buttons(pending)
                if pending.step == InteractionStep.RETRY_PENDING
                else None
            )
            await self._run_cancellation_safe(
                lambda: self._deliver_pending_result(pending, text, buttons=buttons)
            )
            return
        except asyncio.CancelledError:
            if handoff:
                raise
            await self._cleanup_working_after_cancel(
                actor_id,
                claimed,
                prior_step=prior_step,
                prior_prompt_message_id=prior_anchor,
                prior_name=prior_name,
                prior_directory=prior_directory,
                terminal=False,
                recovery_target=None,
                job_id=session.job_id,
                batch=True,
            )
            raise

    async def _restore_working(
        self,
        actor_id: int,
        session: InteractionSession,
        *,
        step: str,
        prompt_message_id: int | None,
        pending_name: str | None,
        pending_directory: str | None,
    ) -> InteractionSession | None:
        assert self.interactions is not None
        return await self.interactions.restore_working_interaction(
            session.id,
            actor_id,
            step=step,
            prompt_message_id=prompt_message_id,
            expires_at=datetime.now(UTC) + _INTERACTION_LIFETIME,
            pending_name=pending_name,
            pending_directory=pending_directory,
        )

    async def _cleanup_working_after_cancel(
        self,
        actor_id: int,
        session: InteractionSession,
        *,
        prior_step: str,
        prior_prompt_message_id: int | None,
        prior_name: str | None,
        prior_directory: str | None,
        terminal: bool,
        recovery_target: tuple[str, str | None, str | None] | None,
        job_id: UUID | None,
        batch: bool,
    ) -> None:
        """Return a claimed workflow to a durable state after cancellation.

        A cancellation before an idempotent operation completes restores the
        exact prior confirmation prompt and anchor. A completed terminal
        operation consumes the interaction instead of restoring a duplicate
        action. If the operation completed but is retryable, the explicit
        recovery target is used.
        """

        async def cleanup() -> None:
            assert self.interactions is not None
            try:
                if terminal:
                    if batch or job_id is None:
                        await self.interactions.delete_interaction(session.id, actor_id)
                    else:
                        await self.interactions.delete_interactions_for_job(job_id)
                    return
                if recovery_target is not None:
                    step, pending_name, pending_directory = recovery_target
                else:
                    step, pending_name, pending_directory = (
                        prior_step,
                        prior_name,
                        prior_directory,
                    )
                await self.interactions.restore_working_interaction(
                    session.id,
                    actor_id,
                    step=step,
                    prompt_message_id=prior_prompt_message_id,
                    expires_at=datetime.now(UTC) + _INTERACTION_LIFETIME,
                    pending_name=pending_name,
                    pending_directory=pending_directory,
                )
            except Exception as error:  # noqa: BLE001 - cancellation must re-raise
                logger.warning(
                    "Telegram working cancellation cleanup failed: outcome=failed error_type=%s",
                    type(error).__name__,
                )

        await self._await_cancellation_safe(cleanup())

    async def _report_claim_loss(
        self,
        actor_id: int,
        session: InteractionSession,
        feedback: _CallbackFeedback,
    ) -> None:
        assert self.interactions is not None
        current = await self.interactions.get_interaction(session.id)
        if current is not None and current.step in _WORKING_STEPS:
            await feedback.respond(_ALREADY_PROCESSING)
            return
        await feedback.respond("This confirmation is stale.")

    async def _persist_single_result(
        self, actor_id: int, claimed: InteractionSession
    ) -> InteractionSession | None:
        """Atomically turn a claimed single-file result into a pending row."""

        assert self.interactions is not None
        return await self.interactions.persist_result_pending(
            claimed.id,
            actor_id,
            action=claimed.action,
            expected_steps=_WORKING_STEPS,
            job_id=claimed.job_id,
        )

    async def _persist_batch_result(
        self,
        actor_id: int,
        claimed: InteractionSession,
        *,
        retry: bool,
    ) -> InteractionSession | None:
        """Atomically turn a claimed batch result into a pending row."""

        assert self.interactions is not None
        if retry:
            return await self.interactions.persist_retry_pending(
                claimed.id,
                actor_id,
                action=claimed.action,
                expected_steps=_WORKING_STEPS,
            )
        return await self.interactions.persist_result_pending(
            claimed.id,
            actor_id,
            action=claimed.action,
            expected_steps=_WORKING_STEPS,
        )

    async def _deliver_pending_result(
        self,
        session: InteractionSession,
        text: str,
        *,
        buttons: Any | None = None,
    ) -> bool:
        """Deliver one pending card and settle it only on a known success."""

        delivered = await self._deliver_pending_message(session, text, buttons=buttons)
        return await self._settle_pending_delivery(session, delivered)

    async def _deliver_pending_message(
        self,
        session: InteractionSession,
        text: str,
        *,
        buttons: Any | None = None,
    ) -> int | None:
        """Edit the stored anchor first, sending once only when uneditable."""

        return await self._prompt_editor.edit(
            session.actor_id,
            session.prompt_message_id,
            text,
            buttons=buttons,
        )

    async def _settle_pending_delivery(
        self, session: InteractionSession, delivered_id: int | None
    ) -> bool:
        """Settle a known delivered pending row with expected-state CAS calls."""

        assert self.interactions is not None
        anchor = session.prompt_message_id
        if delivered_id is not None and delivered_id != anchor:
            bound = await self.interactions.bind_pending_prompt_message_id(
                session.id,
                session.actor_id,
                expected_step=session.step,
                expected_prompt_message_id=anchor,
                prompt_message_id=delivered_id,
            )
            if bound is None:
                logger.warning(
                    "Telegram pending result anchor not bound: outcome=stale-anchor step=%s",
                    session.step,
                )
                return False
            anchor = delivered_id
        if session.step == InteractionStep.RETRY_PENDING:
            updated = await self.interactions.mark_retry_ready(
                session.id,
                session.actor_id,
                action=session.action,
                expected_prompt_message_id=anchor,
                expires_at=datetime.now(UTC) + _INTERACTION_LIFETIME,
            )
            if updated is None:
                logger.warning(
                    "Telegram retry result not actionable: outcome=stale-state step=%s",
                    session.step,
                )
            return updated is not None
        settled = await self.interactions.finish_result_delivery(
            session.id,
            session.actor_id,
            action=session.action,
            expected_step=session.step,
            expected_prompt_message_id=anchor,
        )
        if not settled:
            logger.warning(
                "Telegram result delivery cleanup skipped: outcome=stale-state step=%s",
                session.step,
            )
        return settled

    async def _single_result_text(self, actor_id: int, session: InteractionSession) -> str:
        """Reconstruct a terminal single-file card from current catalog state."""

        header = await self._context_header(actor_id, session)
        if self.library is None or session.job_id is None:
            return f"{header}\n\nCompleted."
        try:
            stored = await self.library.resolve(actor_id, session.job_id.hex)
        except StoredFileNotFoundError:
            return f"{header}\n\nCompleted."
        return f"{header}\n\nCompleted:\n{_format_stored_file(stored)}"

    async def restore_pending_results(self, sessions: Sequence[InteractionSession]) -> int:
        """Best-effort delivery of durable result and retry pending rows.

        Only current catalog and job reads render the cards; no organization or
        download mutation API is called during recovery. Unauthorized rows are
        skipped and retained, and every session failure is isolated.
        """

        if self.interactions is None or not sessions:
            return 0
        visible = 0
        for session in sessions:
            try:
                self.jobs.require_authorized(session.actor_id)
            except UnauthorizedUserError:
                logger.info(
                    "Telegram pending result recovery skipped: outcome=unauthorized step=%s",
                    session.step,
                )
                continue
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - one row cannot stop startup
                logger.warning(
                    "Telegram pending result recovery failed: outcome=failed step=%s error_type=%s",
                    session.step,
                    type(error).__name__,
                )
                continue
            try:
                settled = await self._restore_pending_result(session)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - one row cannot stop startup
                logger.warning(
                    "Telegram pending result recovery failed: outcome=failed step=%s error_type=%s",
                    session.step,
                    type(error).__name__,
                )
                settled = False
            if settled:
                visible += 1
        return visible

    async def _restore_pending_result(self, session: InteractionSession) -> bool:
        text, buttons = await self._pending_result_card(session)
        try:
            delivered = await self._deliver_pending_message(session, text, buttons=buttons)
        except _DeliveryFailure:
            logger.warning(
                "Telegram pending result recovery failed: outcome=not-visible "
                "step=%s category=send",
                session.step,
            )
            return False
        return await self._settle_pending_delivery(session, delivered)

    async def _pending_result_card(
        self, session: InteractionSession
    ) -> tuple[str, list[list[Button]] | None]:
        if session.batch_id is not None:
            header = await self._context_header(session.actor_id, session)
            summary = await self._catalog_batch_summary(session.actor_id, session)
            if session.step == InteractionStep.RETRY_PENDING:
                return (
                    f"{header}\n\n{summary}\n\n{_BATCH_RETRY_NOTICE}",
                    _retry_cancel_buttons(session),
                )
            return f"{header}\n\n{summary}", None
        return await self._single_result_text(session.actor_id, session), None

    async def _catalog_batch_summary(self, actor_id: int, session: InteractionSession) -> str:
        """Reconstruct batch result counts from current authorized catalog state."""

        assert self.library is not None and session.batch_id is not None
        try:
            progress = await self.jobs.batch_progress(actor_id, session.batch_id)
            files = await self.library.files_for_batch(actor_id, session.batch_id)
        except JobNotFoundError, UnauthorizedUserError:
            return "This batch is no longer available."
        archived = sum(1 for item in files if item.location is FileLocation.ARCHIVE)
        remaining = len(files) - archived
        retry = session.step == InteractionStep.RETRY_PENDING
        result = BatchMoveResult(
            batch_id=session.batch_id,
            total_members=progress.total_jobs,
            moved=archived,
            already_moved=0,
            missing=max(0, progress.completed - len(files)),
            not_ready=max(0, progress.total_jobs - progress.completed),
            failed=remaining if retry else 0,
            items=(),
        )
        return _format_batch_result(result)

    async def restore_interaction_cards(self, sessions: Sequence[InteractionSession]) -> int:
        """Best-effort restore visible controls for recovered interactions.

        Each session is isolated: a chat, edit, send, or repository failure is
        logged with only the step, outcome, and error type, never an actor or
        message ID, name, or path. Failure never aborts startup or changes the
        already-restored database state.
        """

        if self.interactions is None or not sessions:
            return 0
        visible = 0
        for session in sessions:
            try:
                self.jobs.require_authorized(session.actor_id)
                restored = await self._restore_interaction_card(session)
            except UnauthorizedUserError:
                logger.info(
                    "Telegram recovery card skipped: outcome=unauthorized step=%s",
                    session.step,
                )
                continue
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - one card cannot stop startup
                logger.warning(
                    "Telegram recovery card failed: outcome=failed step=%s error_type=%s",
                    session.step,
                    type(error).__name__,
                )
                restored = False
            if restored:
                visible += 1
        return visible

    async def _restore_interaction_card(self, session: InteractionSession) -> bool:
        assert self.interactions is not None
        text, buttons = await self._recovery_card(session)
        anchor = session.prompt_message_id
        try:
            new_id = await self._prompt_editor.edit(
                session.actor_id,
                anchor,
                text,
                buttons=buttons,
            )
        except _DeliveryFailure:
            if anchor is None:
                logger.warning(
                    "Telegram recovery card failed: outcome=not-visible step=%s category=send",
                    session.step,
                )
                return False
            # The anchor is provably uneditable: send exactly one replacement
            # and bind it with the same expected-anchor compare-and-swap.
            try:
                new_id = await self._prompt_editor.edit(
                    session.actor_id,
                    None,
                    text,
                    buttons=buttons,
                )
            except _DeliveryFailure:
                logger.warning(
                    "Telegram recovery card failed: outcome=not-visible step=%s category=send",
                    session.step,
                )
                return False
        if new_id is None:
            return True
        try:
            updated = await self.interactions.compare_and_set_prompt_message_id(
                session.id,
                session.actor_id,
                expected_prompt_message_id=anchor,
                prompt_message_id=new_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - disable the unbound card
            logger.warning(
                "Telegram recovery card failed: outcome=not-visible step=%s "
                "category=anchor error_type=%s",
                session.step,
                type(error).__name__,
            )
            await self._disable_stale_message(session.actor_id, new_id)
            return False
        if updated is None:
            logger.warning(
                "Telegram recovery card failed: outcome=not-visible step=%s category=stale-anchor",
                session.step,
            )
            await self._disable_stale_message(session.actor_id, new_id)
            return False
        return True

    async def _recovery_card(
        self,
        session: InteractionSession,
    ) -> tuple[str, list[list[Button]] | None]:
        header = await self._context_header(session.actor_id, session)
        if session.step in _CONFIRM_STEPS:
            return _confirmation_text(session, header), _confirm_buttons(session)
        if session.action == InteractionAction.BATCH_MOVE:
            return f"{header}\n\n{_INTERRUPTED_ACTION}", _retry_cancel_buttons(session)
        return f"{header}\n\n{_INTERRUPTED_ACTION}", _recovery_ready_buttons(session)

    async def _render_interrupted(self, actor_id: int, feedback: _CallbackFeedback) -> None:
        count = await self.jobs.paused_count(actor_id)
        jobs = await self.jobs.paused(actor_id, limit=100)
        lines = [f"Interrupted tasks: {count}"]
        if jobs:
            lines.append("")
            lines.extend(
                f"[{job.short_id}] {job.display_title or job.original_file_name or 'Untitled media'}"
                for job in jobs
            )
            if count > len(jobs):
                lines.append(f"Showing the first {len(jobs)}.")
        else:
            lines.append("No interrupted tasks.")
        await feedback.respond("\n".join(lines))

    async def _continue_interrupted(self, actor_id: int, feedback: _CallbackFeedback) -> None:
        resumed = await self.jobs.continue_paused(actor_id)
        await feedback.respond(f"Resumed {resumed} interrupted task(s).")

    async def send_startup_notice(
        self,
        requester_id: int,
        interrupted_count: int,
        *,
        version: str | None = None,
        now: datetime | None = None,
    ) -> None:
        local_now = datetime.now().astimezone() if now is None else now.astimezone()
        lines = [
            f"TGHarbor {__version__ if version is None else version}",
            f"Server time: {local_now.replace(microsecond=0).isoformat()}",
            f"Interrupted tasks: {interrupted_count}",
        ]
        buttons: list[list[Button]] | None = None
        if interrupted_count > 0 and self.interactions is not None:
            expires_at = datetime.now(UTC) + _INTERACTION_LIFETIME
            view = await self.interactions.create_interaction(
                requester_id,
                InteractionAction.INTERRUPTED_VIEW,
                InteractionStep.READY,
                expires_at=expires_at,
            )
            continuation = await self.interactions.create_interaction(
                requester_id,
                InteractionAction.INTERRUPTED_CONTINUE,
                InteractionStep.READY,
                expires_at=expires_at,
            )
            buttons = [
                [
                    _action_button("View interrupted tasks", view),
                    _action_button("Continue interrupted tasks", continuation),
                ]
            ]
        elif interrupted_count == 0:
            lines.append("No interrupted tasks.")
        await self.client.send_message(requester_id, "\n".join(lines), buttons=buttons)

    async def _page_buttons(
        self,
        actor_id: int,
        action: str,
        page: int,
        *,
        has_next: bool,
        prompt_message_id: int | None,
    ) -> tuple[list[list[Button]] | None, tuple[InteractionSession, ...]]:
        if self.interactions is None:
            return None, ()
        targets = []
        if page > 1:
            targets.append(("Previous", page - 1))
        if has_next:
            targets.append(("Next", page + 1))
        if not targets:
            return None, ()
        expires_at = datetime.now(UTC) + _INTERACTION_LIFETIME
        buttons = []
        sessions = []
        try:
            for label, target in targets:
                session = await self.interactions.create_interaction(
                    actor_id,
                    action,
                    str(target),
                    expires_at=expires_at,
                    # Successor rows start on the message that carries the
                    # successor buttons. Edit-first delivery therefore needs no
                    # rebind; only a fallback send moves the anchor.
                    prompt_message_id=prompt_message_id,
                )
                sessions.append(session)
                buttons.append(
                    Button.inline(label, data=f"tg:{session.id}:{session.action}".encode())
                )
        except BaseException:
            await self._await_cancellation_safe(self._discard_sessions(tuple(sessions)))
            raise
        return [buttons], tuple(sessions)

    async def _keep_interaction(
        self,
        actor_id: int,
        session: InteractionSession,
        feedback: _CallbackFeedback,
    ) -> None:
        assert session.job_id is not None and self.interactions is not None
        job = None
        stored = None
        try:
            job = await self.jobs.get(actor_id, session.job_id)
            if self.library is not None:
                stored = await self.library.resolve(actor_id, session.job_id.hex)
        except JobNotFoundError, StoredFileNotFoundError, UnauthorizedUserError:
            job = None
        await self.interactions.delete_interactions_for_job(session.job_id)
        if job is None:
            summary = f"Job {session.job_id.hex[:8]}"
        else:
            summary = _completion_message(job, stored)
        await feedback.deliver(f"{summary}\n\n{_KEPT_NOTICE}")

    async def _confirm_interaction(
        self,
        actor_id: int,
        session: InteractionSession,
        feedback: _CallbackFeedback,
    ) -> None:
        if (
            (session.step not in _CONFIRM_STEPS and session.step not in _WORKING_STEPS)
            or self.library is None
            or self.interactions is None
        ):
            await feedback.respond("This confirmation is stale.")
            return
        assert session.job_id is not None
        directory = session.pending_directory
        header = await self._context_header(actor_id, session)
        if session.step == InteractionStep.CONFIRM_NEW_DIRECTORY and not directory:
            await self._deliver_anchored_card(
                actor_id,
                session,
                feedback,
                f"{header}\n\n{_INVALID_INPUT}",
                buttons=_back_cancel_buttons(session),
            )
            return
        if directory:
            try:
                validate_relative_directory(directory)
            except ValueError, RuntimeError:
                await self._deliver_anchored_card(
                    actor_id,
                    session,
                    feedback,
                    f"{header}\n\n{_INVALID_INPUT}",
                    buttons=_back_cancel_buttons(session),
                )
                return
        claimed = await self.interactions.claim_working_interaction(
            session.id,
            actor_id,
            action=session.action,
            expected_steps=_CONFIRM_STEPS,
        )
        if claimed is None:
            await self._report_claim_loss(actor_id, session, feedback)
            return
        prior_step = session.step
        prior_anchor = claimed.prompt_message_id
        prior_name = session.pending_name
        prior_directory = session.pending_directory
        handoff = False

        async def commit_and_handoff() -> InteractionSession | None:
            """Create/organize and persist one committed result atomically.

            This whole body runs under a cancellation shield, so a single-file
            move that committed in storage or catalog can never be followed by a
            restored confirmation. Pre-commit failures still restore the prior
            confirmation and render their existing recoverable controls.
            """

            assert self.interactions is not None
            if prior_step == InteractionStep.CONFIRM_NEW_DIRECTORY:
                assert directory is not None
                try:
                    await self.library.mkdir(actor_id, directory)
                except (OSError, RuntimeError, ValueError) as error:
                    _log_library_failure("mkdir", error)
                    restored = await self._restore_working(
                        actor_id,
                        claimed,
                        step=prior_step,
                        prompt_message_id=prior_anchor,
                        pending_name=prior_name,
                        pending_directory=prior_directory,
                    )
                    retained = restored or session
                    await self._deliver_anchored_card(
                        actor_id,
                        retained,
                        feedback,
                        f"{header}\n\n{_DIRECTORY_CREATE_FAILURE}",
                        buttons=_back_cancel_buttons(retained),
                    )
                    return None
            try:
                await self.library.organize(
                    actor_id,
                    session.job_id.hex,
                    new_name=session.pending_name,
                    archive_directory=directory,
                )
            except StoredFileNotFoundError as error:
                _log_library_failure("organize", error)
                await self.interactions.delete_interactions_for_job(session.job_id)
                await feedback.deliver(f"{header}\n\n{_STALE_FILE}")
                return None
            except ValueError as error:
                _log_library_failure("organize", error)
                restored = await self._restore_working(
                    actor_id,
                    claimed,
                    step=prior_step,
                    prompt_message_id=prior_anchor,
                    pending_name=prior_name,
                    pending_directory=prior_directory,
                )
                retained = restored or session
                await self._deliver_anchored_card(
                    actor_id,
                    retained,
                    feedback,
                    f"{header}\n\n{_INVALID_INPUT}",
                    buttons=_back_cancel_buttons(retained),
                )
                return None
            except (OSError, RuntimeError) as error:
                _log_library_failure("organize", error)
                restored = await self._restore_working(
                    actor_id,
                    claimed,
                    step=prior_step,
                    prompt_message_id=prior_anchor,
                    pending_name=prior_name,
                    pending_directory=prior_directory,
                )
                retained = restored or session
                await self._deliver_anchored_card(
                    actor_id,
                    retained,
                    feedback,
                    f"{header}\n\n{_ORGANIZE_FAILURE}",
                    buttons=_back_cancel_buttons(retained),
                )
                return None
            pending = await self._persist_single_result(actor_id, claimed)
            if pending is None:
                # The claim changed; deliver the result once without consuming
                # or overwriting another owner's row.
                await feedback.deliver(await self._single_result_text(actor_id, session))
            return pending

        try:
            try:
                working_message_id = await feedback.deliver(f"{header}\n\n{_WORKING_FILE}")
            except _DeliveryFailure:
                await self._restore_working(
                    actor_id,
                    claimed,
                    step=prior_step,
                    prompt_message_id=prior_anchor,
                    pending_name=prior_name,
                    pending_directory=prior_directory,
                )
                raise
            await self._bind_prompt(
                claimed,
                actor_id,
                working_message_id,
                expected=prior_anchor,
            )
            handoff = True
            pending = await self._run_cancellation_safe(commit_and_handoff)
            if pending is None:
                return
            text = await self._single_result_text(actor_id, pending)
            await self._run_cancellation_safe(lambda: self._deliver_pending_result(pending, text))
            return
        except asyncio.CancelledError:
            if handoff:
                raise
            await self._cleanup_working_after_cancel(
                actor_id,
                claimed,
                prior_step=prior_step,
                prior_prompt_message_id=prior_anchor,
                prior_name=prior_name,
                prior_directory=prior_directory,
                terminal=False,
                recovery_target=None,
                job_id=session.job_id,
                batch=False,
            )
            raise


class TelegramJobNotifier:
    def __init__(
        self,
        bot_client: Any,
        repository: NotificationRepository | None = None,
        *,
        batch_dispatcher_wake: Callable[[], None] | None = None,
    ) -> None:
        self.bot_client = bot_client
        self.repository = repository
        self._batch_dispatcher_wake = batch_dispatcher_wake

    def bind_batch_dispatcher(self, wake: Callable[[], None]) -> None:
        self._batch_dispatcher_wake = wake

    async def job_finished(self, job: DownloadJob) -> None:
        if job.status is not JobStatus.COMPLETED or self.repository is None:
            await self.bot_client.send_message(job.requester_id, _format_job(job))
            return
        stored = await self._stored_file(job)
        try:
            buttons = await self._completion_buttons(job)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - the summary remains deliverable
            _log_notification_failure("completion-actions", error)
            buttons = None
        await self.bot_client.send_message(
            job.requester_id,
            _completion_message(job, stored),
            buttons=buttons,
        )
        if job.batch_id is not None and job.batch_expected_size > 1:
            delivered = await self.deliver_batch_notice(job.batch_id, job.requester_id)
            if not delivered:
                self._wake_batch_dispatcher()

    def _wake_batch_dispatcher(self) -> None:
        wake = self._batch_dispatcher_wake
        if wake is None:
            return
        try:
            wake()
        except Exception as error:  # noqa: BLE001 - dispatching stays non-fatal
            _log_notification_failure("batch-wake", error)

    async def _stored_file(self, job: DownloadJob) -> StoredFile | None:
        assert self.repository is not None
        try:
            return await self.repository.get_file(job.id)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - notification remains best effort
            logger.warning(
                "Ignoring %s while reading completion metadata for job %s",
                error.__class__.__name__,
                job.id,
            )
            return None

    async def _completion_buttons(self, job: DownloadJob) -> list[list[Button]] | None:
        if self.repository is None:
            return None
        expires_at = datetime.now(UTC) + _INTERACTION_LIFETIME
        sessions = []
        for action in (
            InteractionAction.KEEP,
            InteractionAction.RENAME,
            InteractionAction.ARCHIVE,
            InteractionAction.RENAME_AND_ARCHIVE,
        ):
            sessions.append(
                await self.repository.create_interaction(
                    job.requester_id,
                    action,
                    InteractionStep.READY,
                    expires_at=expires_at,
                    job_id=job.id,
                )
            )
        return [
            [
                _action_button("Keep", sessions[0]),
                _action_button("Rename", sessions[1]),
            ],
            [
                _action_button("Archive", sessions[2]),
                _action_button("Rename + archive", sessions[3]),
            ],
        ]

    async def deliver_batch_notice(
        self,
        batch_id: str,
        requester_id: int,
        *,
        stale_after_seconds: float | None = None,
    ) -> bool:
        """Claim and deliver one durable batch notice.

        Returns True only after the notice reaches its terminal ``sent`` state.
        A send or mark failure releases the claim so the dispatcher can retry.
        The same interaction row, and therefore the same callback identity, is
        reused across attempts. Delivery is at-least-once: a crash after the
        Telegram send but before the SQLite mark can duplicate the chat message.

        ``stale_after_seconds`` is supplied by the dispatcher so its configured
        threshold governs the atomic reclaim as well as candidate discovery.
        Direct job-completion delivery omits it and keeps the repository default.
        """

        if self.repository is None:
            return False
        claim = f"notifier-{uuid4().hex}"
        try:
            if stale_after_seconds is None:
                claimed = await self.repository.claim_batch_notification(
                    batch_id,
                    requester_id,
                    claimed_by=claim,
                )
            else:
                claimed = await self.repository.claim_batch_notification(
                    batch_id,
                    requester_id,
                    claimed_by=claim,
                    stale_after_seconds=stale_after_seconds,
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - notification remains best effort
            _log_notification_failure("batch-claim", error)
            return False
        if not claimed:
            return False
        sent = False
        try:
            progress = await self.repository.batch_progress_for_requester(
                requester_id,
                batch_id,
            )
            if progress is None or not progress.all_succeeded:
                await self.repository.release_batch_notification(
                    batch_id,
                    requester_id,
                    claimed_by=claim,
                )
                return False
            session = await self.repository.ensure_batch_move_interaction(
                requester_id,
                batch_id,
                expires_at=datetime.now(UTC) + _INTERACTION_LIFETIME,
            )
            button = Button.inline(
                _MOVE_ALL_LABEL,
                data=f"tg:{session.id}:{InteractionAction.BATCH_MOVE}".encode(),
            )
            await self.bot_client.send_message(
                requester_id,
                _batch_message(progress),
                buttons=[[button]],
            )
            sent = True
        except asyncio.CancelledError:
            if not sent:
                await self._release_batch_notice(batch_id, requester_id, claim)
            raise
        except Exception as error:  # noqa: BLE001 - notification remains best effort
            _log_notification_failure("batch-send", error)
            if not sent:
                await self._release_batch_notice(batch_id, requester_id, claim)
            return False
        if await self._mark_batch_notice_sent(batch_id, requester_id, claimed_by=claim):
            return True
        # The send happened but durability did not. Releasing makes the retry
        # prompt while keeping the existing interaction and callback identity.
        await self._release_batch_notice(batch_id, requester_id, claim)
        return False

    async def _release_batch_notice(
        self, batch_id: str, requester_id: int, claimed_by: str
    ) -> None:
        if self.repository is None:
            return
        with contextlib.suppress(Exception):
            await self.repository.release_batch_notification(
                batch_id,
                requester_id,
                claimed_by=claimed_by,
            )

    async def _mark_batch_notice_sent(
        self, batch_id: str, requester_id: int, *, claimed_by: str
    ) -> bool:
        assert self.repository is not None
        try:
            return await self.repository.mark_batch_notification_sent(
                batch_id,
                requester_id,
                claimed_by=claimed_by,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - delivery already happened
            _log_notification_failure("batch-mark-sent", error)
            return False


class BatchNotificationDispatcher:
    """Durably redrive eligible batch notices while the service is running."""

    def __init__(
        self,
        sender: BatchNoticeSender,
        *,
        poll_interval: float = 15.0,
        batch_limit: int = 20,
        stale_after_seconds: float = 300.0,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if batch_limit <= 0:
            raise ValueError("batch_limit must be positive")
        if stale_after_seconds < 0:
            raise ValueError("stale_after_seconds must not be negative")
        self.sender = sender
        self.poll_interval = poll_interval
        self.batch_limit = batch_limit
        self.stale_after_seconds = stale_after_seconds
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def notify(self) -> None:
        self._wake.set()

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("batch notification dispatcher is already started")
        self._task = asyncio.create_task(
            self._run(),
            name="tgharbor-batch-notifier",
        )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def dispatch_once(self) -> int:
        repository = self.sender.repository
        if repository is None:
            return 0
        try:
            candidates = await repository.list_claimable_batch_notifications(
                stale_after_seconds=self.stale_after_seconds,
                limit=self.batch_limit,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - the outbox keeps retrying
            _log_notification_failure("batch-list", error)
            return 0
        delivered = 0
        for notice in candidates:
            try:
                if await self.sender.deliver_batch_notice(
                    notice.batch_id,
                    notice.requester_id,
                    stale_after_seconds=self.stale_after_seconds,
                ):
                    delivered += 1
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - one notice cannot stop delivery
                _log_notification_failure("batch-deliver", error)
        return delivered

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval)
            except TimeoutError:
                pass
            self._wake.clear()
            try:
                await self.dispatch_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - the polling loop stays alive
                _log_notification_failure("batch-dispatch", error)


def _accepted(job: DownloadJob) -> str:
    source = "Bot upload" if job.source_session is SourceSession.BOT else "Telegram message link"
    return (
        f"Download queued · {job.short_id}\n"
        f"Source: {source}\n"
        f"Status: {job.status.value}\n"
        f"Next: /status {job.short_id}"
    )


def _album_batch_identifier(
    identity: str,
    requester_id: int,
    source_chat: str,
    grouped_id: int | str,
) -> str:
    material = "\x00".join(
        (
            _ALBUM_BATCH_VERSION,
            str(requester_id),
            identity,
            str(source_chat),
            str(grouped_id),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _album_source_metadata(
    messages: list[Any],
) -> tuple[str | None, str | None]:
    peers: list[tuple[str, str | None]] = []
    for message in messages:
        forward = getattr(message, "forward", None)
        if forward is None:
            return None, None
        chat_id = getattr(forward, "chat_id", None)
        if chat_id is None:
            return None, None
        if getattr(forward, "is_group", False):
            peers.append((str(chat_id), None))
        elif getattr(forward, "is_channel", False):
            peers.append((str(chat_id), str(chat_id)))
        else:
            return None, None
    if not peers or any(peer != peers[0] for peer in peers[1:]):
        return None, None
    return peers[0]


def _completion_message(job: DownloadJob, stored: StoredFile | None) -> str:
    file_name = (
        stored.current_name
        if stored is not None
        else (job.reserved_name or job.original_file_name or job.destination_name or "N/A")
    )
    folder = _logical_folder(stored.relative_path) if stored is not None else "N/A"
    size_bytes = stored.size_bytes if stored is not None else job.total_bytes
    elapsed = _elapsed_seconds(job)
    lines = [
        "Download Completed",
        f"Job: {job.short_id}",
        f"File Name: {file_name}",
        f"Download Folder: {folder}",
        f"File Size: {_format_mib(size_bytes)} MB",
        f"Start Time: {_local_clock(job.started_at)}",
        f"End Time: {_local_clock(job.finished_at)}",
        f"Download Time: {_format_elapsed(elapsed)}",
        f"Download Speed: {_format_speed(size_bytes, elapsed)} KB/s",
        f"User Id: {job.requester_id}",
        f"Origin Group: {job.origin_group_id or 'N/A'}",
        f"Channel ID: {job.channel_id or 'N/A'}",
    ]
    return "\n".join(lines)


def _logical_folder(relative: Path) -> str:
    parent = relative.parent.as_posix()
    if parent in {"", "."}:
        return "/download"
    return f"/download/{parent}"


def _elapsed_seconds(job: DownloadJob) -> float | None:
    if job.started_at is None or job.finished_at is None:
        return None
    elapsed = (job.finished_at - job.started_at).total_seconds()
    return elapsed if elapsed > 0 else None


def _format_mib(value: int | None) -> str:
    if value is None or value < 0:
        return "N/A"
    return f"{value / (1024 * 1024):.2f}"


def _format_elapsed(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2f}"


def _format_speed(size_bytes: int | None, elapsed: float | None) -> str:
    if size_bytes is None or size_bytes < 0 or elapsed is None or elapsed <= 0:
        return "N/A"
    return f"{size_bytes / elapsed / 1024:.2f}"


def _local_clock(value: datetime | None) -> str:
    if value is None:
        return "N/A"
    return value.astimezone().strftime("%H:%M:%S")


def _batch_message(progress: BatchProgress) -> str:
    return (
        "Album download completed\n"
        f"Files: {progress.completed}\n"
        "All files are ready to move together."
    )


def _format_batch_result(result: BatchMoveResult) -> str:
    skipped = result.already_moved + result.missing + result.not_ready
    return "\n".join(
        (
            "Batch move result:",
            f"Moved: {result.moved}",
            f"Skipped: {skipped}",
            f"Failed: {result.failed}",
            f"Already moved: {result.already_moved}",
            f"Missing: {result.missing}",
            f"Not ready: {result.not_ready}",
        )
    )


def _format_job(job: DownloadJob) -> str:
    title = job.display_title or job.original_file_name or job.reserved_name or "Untitled media"
    lines = [f"[{job.short_id}] {title}", f"Job: {job.short_id}", f"Status: {job.status.value}"]
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


async def register_commands(client: Any) -> None:
    """Best-effort Telegram command menu registration.

    Default and all-private-chat scopes are registered independently. A scope
    failure is logged by outcome, scope, and error type only, and never blocks
    typed commands or startup.
    """

    commands = [
        BotCommand(command=name, description=description) for name, description in _BOT_COMMANDS
    ]
    scopes = (
        ("default", BotCommandScopeDefault()),
        ("all-private", BotCommandScopeUsers()),
    )
    for scope_name, scope in scopes:
        try:
            await client(SetBotCommandsRequest(scope=scope, lang_code="", commands=commands))
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - the command menu is optional
            logger.warning(
                "Telegram command registration failed: outcome=failed scope=%s error_type=%s",
                scope_name,
                type(error).__name__,
            )
        else:
            logger.info("Telegram command registration: outcome=registered scope=%s", scope_name)


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


def _callback_token_matches(session: InteractionSession, token: str) -> bool:
    if token.startswith("directory:"):
        return (
            session.action in _DIRECTORY_ACTIONS
            and session.step == InteractionStep.AWAIT_DIRECTORY
            and _directory_selection_digest(token) is not None
        )
    if token == "new-directory":
        return (
            session.action in _DIRECTORY_ACTIONS and session.step == InteractionStep.AWAIT_DIRECTORY
        )
    if token == "confirm":
        return session.step in _CONFIRM_STEPS or session.step in _WORKING_STEPS
    if token == "back":
        return session.step in _CONFIRM_STEPS
    if token == "cancel":
        return session.step in _CANCELLABLE_STEPS
    if session.action in _PAGE_ACTIONS:
        return token == session.action and _positive_int(session.step) is not None
    return (
        token == session.action
        and session.action in _ACTION_TOKENS
        and session.step == InteractionStep.READY
    )


def _positive_int(value: str) -> int | None:
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _directory_choice_prompt(
    session: InteractionSession,
    directories: list[Path],
    header: str,
    *,
    error: str | None = None,
) -> tuple[str, list[list[Button]]]:
    """Render a contextual directory chooser bound to the displayed ordering."""

    displayed = directories[:_DIRECTORY_PAGE_SIZE]
    buttons = [
        [
            Button.inline(
                str(directory),
                data=_directory_callback_data(session, _directory_digest(directory, displayed)),
            )
        ]
        for directory in displayed
    ]
    buttons.append([Button.inline("New directory", data=f"tg:{session.id}:new-directory".encode())])
    buttons.append([_cancel_button(session)])
    lines = [header, ""]
    if error:
        lines.extend((error, ""))
    lines.append(
        "Choose where to move all files in this album."
        if session.action == InteractionAction.BATCH_MOVE
        else "Choose where to move this file."
    )
    if displayed:
        lines.append("Existing directories, most recently used first:")
        lines.extend(f"• {directory}" for directory in displayed)
    else:
        lines.append("No archive directories exist yet.")
    lines.append("Send a relative path as text, or use the buttons below.")
    return "\n".join(lines), buttons


def _folder_prompt_text(header: str, *, error: str | None = None) -> str:
    lines = [header, ""]
    if error:
        lines.extend((error, ""))
    lines.append(_FOLDER_PROMPT)
    return "\n".join(lines)


def _directory_callback_data(session: InteractionSession, digest: str) -> bytes:
    data = f"tg:{session.id}:directory:{digest}".encode()
    if len(data) > _TELEGRAM_CALLBACK_LIMIT:
        raise ValueError("directory callback data exceeds the Telegram limit")
    return data


def _directory_digest(directory: Path, displayed: list[Path]) -> str:
    """Return an opaque digest bound to one directory and the displayed ordering."""

    material = "\x00".join(
        (_DIRECTORY_TOKEN_VERSION, directory.as_posix(), *(item.as_posix() for item in displayed))
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:_DIRECTORY_DIGEST_LENGTH]


def _directory_selection_digest(token: str) -> str | None:
    prefix, separator, value = token.partition(":")
    if prefix != "directory" or separator != ":" or len(value) != _DIRECTORY_DIGEST_LENGTH:
        return None
    if any(character not in "0123456789abcdef" for character in value):
        return None
    return value


def _match_directory_selection(token: str, directories: list[Path]) -> Path | None:
    """Resolve a rendered selection only while its binding still holds.

    A reordered or changed directory list invalidates every previously rendered
    button, so a click can never silently resolve to a different directory.
    """

    digest = _directory_selection_digest(token)
    if digest is None:
        return None
    displayed = directories[:_DIRECTORY_PAGE_SIZE]
    for directory in displayed:
        if hmac.compare_digest(_directory_digest(directory, displayed), digest):
            return directory
    return None


def _log_library_failure(operation: str, error: BaseException) -> None:
    logger.warning(
        "Telegram library action failed: outcome=failed operation=%s error_type=%s",
        operation,
        type(error).__name__,
    )


def _log_notification_failure(operation: str, error: BaseException) -> None:
    logger.warning(
        "Telegram notification failed: outcome=failed operation=%s error_type=%s",
        operation,
        type(error).__name__,
    )


def _action_button(label: str, session: InteractionSession) -> Button:
    return Button.inline(label, data=f"tg:{session.id}:{session.action}".encode())


def _cancel_button(session: InteractionSession) -> Button:
    return Button.inline(_CANCEL_LABEL, data=f"tg:{session.id}:cancel".encode())


def _back_cancel_buttons(session: InteractionSession) -> list[list[Button]]:
    return [
        [
            Button.inline(_BACK_LABEL, data=f"tg:{session.id}:back".encode()),
            _cancel_button(session),
        ]
    ]


def _retry_cancel_buttons(session: InteractionSession) -> list[list[Button]]:
    return [
        [
            _action_button(_RETRY_MOVE_LABEL, session),
            _cancel_button(session),
        ]
    ]


def _recovery_ready_buttons(session: InteractionSession) -> list[list[Button]] | None:
    """Render a safe retry or choose-folder action for a recovered READY row."""

    label = _READY_ACTION_LABELS.get(session.action)
    if label is None:
        if session.step in _CANCELLABLE_STEPS:
            return [[_cancel_button(session)]]
        return None
    return [[_action_button(label, session), _cancel_button(session)]]


def _confirm_buttons(session: InteractionSession) -> list[list[Button]]:
    label = (
        _CONFIRM_NEW_LABEL
        if session.step == InteractionStep.CONFIRM_NEW_DIRECTORY
        else _CONFIRM_EXISTING_LABEL
    )
    return [
        [
            Button.inline(label, data=f"tg:{session.id}:confirm".encode()),
            Button.inline(_BACK_LABEL, data=f"tg:{session.id}:back".encode()),
            _cancel_button(session),
        ]
    ]


def _confirmation_text(session: InteractionSession, header: str) -> str:
    heading = (
        "Confirm batch move:"
        if session.action == InteractionAction.BATCH_MOVE
        else "Confirm file organization:"
    )
    lines = [header, "", heading]
    if session.pending_name:
        lines.append(f"Name: {session.pending_name}")
    if session.pending_directory:
        suffix = (
            " (will be created)" if session.step == InteractionStep.CONFIRM_NEW_DIRECTORY else ""
        )
        lines.append(f"Directory: {session.pending_directory}{suffix}")
    if session.step == InteractionStep.CONFIRM_NEW_DIRECTORY:
        lines.append("Next: Create and move creates the folder and moves the file.")
    elif session.pending_directory:
        lines.append("Next: Move here moves the file into this directory.")
    return "\n".join(lines)


class _CallbackFeedback:
    """Acknowledge one callback once and deliver persistent feedback.

    Callback editing and sending use the underlying client APIs, never
    ``CallbackQuery.Event.edit`` or ``respond``: Telethon 1.45 implicitly answers
    from both shorthands, which would acknowledge an already-acknowledged query.
    """

    def __init__(self, event: Any, client: Any | None = None) -> None:
        self.event = event
        self.client = client if client is not None else getattr(event, "_client", None)
        self._acknowledged = False
        self._chat_id = _callback_chat_id(event)
        self._message_id = _callback_message_identifier(event)
        self.original_text = _callback_original_text(event)

    @property
    def message_id(self) -> int | None:
        """The message that carried this callback before any delivery."""

        return self._message_id

    async def acknowledge(self) -> None:
        if self._acknowledged:
            return
        self._acknowledged = True
        answer = getattr(self.event, "answer", None)
        if answer is None:
            return
        try:
            await answer()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - acknowledgement is best effort
            logger.warning("Telegram callback acknowledgement failed; continuing")

    async def respond(self, text: str, *, buttons: Any | None = None) -> int | None:
        if _text_too_long(text):
            _log_delivery_failure("too-long", "send")
            raise _DeliveryFailure("too-long")
        send_message = getattr(self.client, "send_message", None)
        if send_message is None or self._chat_id is None:
            _log_delivery_failure("unavailable", "send")
            raise _DeliveryFailure("send")
        try:
            message = await send_message(self._chat_id, text, buttons=buttons)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _log_delivery_failure(type(error).__name__, "send")
            raise _DeliveryFailure("send") from error
        return _message_identifier(message)

    async def deliver(self, text: str, *, buttons: Any | None = None) -> int | None:
        """Edit the current card, falling back to one send only when uneditable."""

        started = perf_counter()
        origin = self._message_id
        if origin is None or self._chat_id is None:
            _log_delivery_failure("unavailable", "edit")
            raise _DeliveryFailure("edit")
        if _text_too_long(text):
            _log_delivery_failure("too-long", "edit")
            raise _DeliveryFailure("too-long")
        edit_message = getattr(self.client, "edit_message", None)
        if edit_message is None:
            sent = await self.respond(text, buttons=buttons)
            if sent is None:
                raise _DeliveryFailure("send")
            self._message_id = sent
            return sent
        try:
            message = await edit_message(self._chat_id, origin, text, buttons=buttons)
        except asyncio.CancelledError:
            raise
        except MessageNotModifiedError:
            _log_delivery("unchanged", "edit", started)
            return origin
        except _EDIT_FALLBACK_ERRORS as error:
            _log_delivery_fallback(error, started)
            sent = await self.respond(text, buttons=buttons)
            if sent is None:
                raise _DeliveryFailure("send") from error
            self._message_id = sent
            return sent
        except Exception as error:
            _log_delivery_failure(type(error).__name__, "edit")
            raise _DeliveryFailure("edit") from error
        _log_delivery("edited", "edit", started)
        self._message_id = _message_identifier(message) or origin
        return self._message_id


class _PromptMessageEditor:
    """Edit a stored prompt in the actor's private chat, or send it once."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def edit(
        self,
        actor_id: int,
        message_id: int | None,
        text: str,
        *,
        buttons: Any | None = None,
    ) -> int | None:
        """Return a new message ID only when a fallback message was sent."""

        started = perf_counter()
        if _text_too_long(text):
            _log_delivery_failure("too-long", "edit")
            raise _DeliveryFailure("too-long")
        edit_message = getattr(self.client, "edit_message", None)
        if message_id is None or edit_message is None:
            return await self._send(actor_id, text, buttons=buttons, started=started)
        try:
            await edit_message(actor_id, message_id, text, buttons=buttons)
        except asyncio.CancelledError:
            raise
        except MessageNotModifiedError:
            _log_delivery("unchanged", "edit", started)
            return None
        except _EDIT_FALLBACK_ERRORS as error:
            _log_delivery_fallback(error, started)
            return await self._send(actor_id, text, buttons=buttons, started=started)
        except Exception as error:
            _log_delivery_failure(type(error).__name__, "edit")
            raise _DeliveryFailure("edit") from error
        _log_delivery("edited", "edit", started)
        return None

    async def _send(
        self,
        actor_id: int,
        text: str,
        *,
        buttons: Any | None,
        started: float,
    ) -> int | None:
        if _text_too_long(text):
            _log_delivery_failure("too-long", "send")
            raise _DeliveryFailure("too-long")
        send_message = getattr(self.client, "send_message", None)
        if send_message is None:
            _log_delivery_failure("unavailable", "send")
            raise _DeliveryFailure("send")
        try:
            message = await send_message(actor_id, text, buttons=buttons)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _log_delivery_failure(type(error).__name__, "send")
            raise _DeliveryFailure("send") from error
        message_id = _message_identifier(message)
        if message_id is None:
            # A successful RPC return without a positive message identity is an
            # ambiguous delivery: the card may be visible but its anchor cannot
            # be bound, so the pending row must remain recoverable.
            _log_delivery_failure("missing-id", "send")
            raise _DeliveryFailure("send")
        _log_delivery("sent", "send", started)
        return message_id


def _message_identifier(message: Any) -> int | None:
    value = getattr(message, "id", None)
    return value if isinstance(value, int) and value > 0 else None


def _callback_message_identifier(event: Any) -> int | None:
    message_id = getattr(event, "message_id", None)
    if isinstance(message_id, int) and message_id > 0:
        return message_id
    return _message_identifier(getattr(event, "message", None))


def _callback_original_text(event: Any) -> str | None:
    """Return the visible card text a callback was rendered in, if available."""

    message = getattr(event, "message", None)
    text = getattr(message, "text", None)
    if text is None:
        text = getattr(message, "message", None)
    if isinstance(text, str) and text.strip() and len(text) <= _TELEGRAM_TEXT_LIMIT:
        return text
    return None


def _callback_chat_id(event: Any) -> int | None:
    chat_id = getattr(event, "chat_id", None)
    if isinstance(chat_id, int):
        return chat_id
    message_chat_id = getattr(getattr(event, "message", None), "chat_id", None)
    if isinstance(message_chat_id, int):
        return message_chat_id
    sender_id = getattr(event, "sender_id", None)
    return sender_id if isinstance(sender_id, int) else None


def _callback_anchor_matches(event: Any, session: InteractionSession) -> bool:
    anchor = session.prompt_message_id
    if anchor is None:
        return True
    return _callback_message_identifier(event) == anchor


def _text_too_long(text: str) -> bool:
    return len(text) > _TELEGRAM_TEXT_LIMIT


def _log_delivery(outcome: str, delivery: str, started: float) -> None:
    logger.debug(
        "Telegram delivery: outcome=%s delivery=%s latency_ms=%.1f",
        outcome,
        delivery,
        (perf_counter() - started) * 1000,
    )


def _log_delivery_fallback(error: BaseException, started: float) -> None:
    logger.warning(
        "Telegram delivery: outcome=fallback delivery=send error_type=%s latency_ms=%.1f",
        type(error).__name__,
        (perf_counter() - started) * 1000,
    )


def _log_delivery_failure(category: str, delivery: str) -> None:
    logger.warning(
        "Telegram delivery: outcome=failed delivery=%s category=%s",
        delivery,
        category if category in {"too-long", "unavailable", "missing-id"} else "rpc-error",
    )
