from __future__ import annotations

import logging
from datetime import UTC, datetime

from tgharbor.app.instance_lock import InstanceLock
from tgharbor.config import AppConfig, load_config
from tgharbor.core.job_api import JobAPI
from tgharbor.core.jobs import SourceSession
from tgharbor.core.library_api import LibraryAPI, recover_pending_organizations
from tgharbor.core.reconciliation import LegacyReconciler
from tgharbor.core.worker import DownloadWorker, WorkerPool, WorkerSettings
from tgharbor.persistence import SQLiteJobQueue
from tgharbor.storage import LocalStorage
from tgharbor.telegram.bot import (
    INTERACTION_LIFETIME,
    BatchNotificationDispatcher,
    TelegramBotController,
    TelegramJobNotifier,
    register_commands,
)
from tgharbor.telegram.provider import TelethonMediaProvider
from tgharbor.telegram.sessions import (
    TelegramClients,
    build_clients,
    start_service_clients,
)

logger = logging.getLogger(__name__)


async def run(
    config: AppConfig | None = None,
    *,
    clients: TelegramClients | None = None,
) -> None:
    """Assemble, run, and cleanly stop the TGHarbor service."""
    active_config = load_config() if config is None else config
    lock = InstanceLock(active_config.database.path)
    pool: WorkerPool | None = None
    controller: TelegramBotController | None = None
    batch_dispatcher: BatchNotificationDispatcher | None = None
    active_clients = clients
    lock.acquire()
    try:
        queue = SQLiteJobQueue(active_config.database.path)
        await queue.initialize()
        # The instance lock proves no previous sender is still alive, so every
        # persisted notice claim can be redriven immediately at startup.
        recovered, exhausted = await queue.recover_interrupted(
            notice_stale_after_seconds=0,
        )
        if recovered or exhausted:
            logger.info(
                "Restart recovery requeued %d and failed %d jobs",
                recovered,
                exhausted,
            )
        storage = LocalStorage(active_config.downloads.path)
        await storage.initialize()
        organization_recovery = await recover_pending_organizations(queue, storage)
        if organization_recovery.discovered:
            logger.info(
                "Organization recovery discovered %d, completed %d, and left %d for review",
                organization_recovery.discovered,
                organization_recovery.recovered,
                organization_recovery.failed,
            )
        reconciliation = await LegacyReconciler(queue, storage).run()
        if reconciliation.discovered:
            logger.info(
                "Legacy storage reconciliation discovered %d, completed %d, and left %d for review",
                reconciliation.discovered,
                reconciliation.reconciled,
                reconciliation.failed,
            )
        # A hard stop can leave a confirmation claimed as a working step, and a
        # long outage can leave that claim past its original expiry. Reconcile
        # storage first, then restore every working row to its explicit prior
        # step with a renewed actionable lifetime so the card stays clickable.
        recovered_interactions = await queue.recover_working_interactions(
            expires_at=datetime.now(UTC) + INTERACTION_LIFETIME,
        )
        if recovered_interactions:
            logger.info(
                "Restart recovery restored %d working interactions",
                len(recovered_interactions),
            )
        # Undelivered result and retry cards are reconstructed only after storage
        # organization reconciliation, and from current catalog state. Their rows
        # are read rather than mutated, so no library operation is rerun. All
        # pending rows are listed; there is no fixed backlog cap that could starve
        # later rows behind a repeatedly failing first one.
        pending_results = await queue.list_pending_delivery_interactions()
        if active_clients is None:
            active_clients = build_clients(active_config)
        await start_service_clients(active_config, active_clients)

        notifier = TelegramJobNotifier(active_clients.bot, queue)
        batch_dispatcher = BatchNotificationDispatcher(notifier)
        notifier.bind_batch_dispatcher(batch_dispatcher.notify)
        await batch_dispatcher.start()
        await batch_dispatcher.dispatch_once()
        worker = DownloadWorker(
            queue,
            {
                SourceSession.BOT: TelethonMediaProvider(active_clients.bot),
                SourceSession.USER: TelethonMediaProvider(active_clients.user),
            },
            storage,
            WorkerSettings(
                timeout_seconds=active_config.downloads.timeout_seconds,
                retry_base_seconds=active_config.downloads.retry_base_seconds,
            ),
            notifier,
        )
        pool = WorkerPool(worker, active_config.downloads.concurrency)
        jobs = JobAPI(
            queue,
            active_config.telegram.authorized_users,
            max_attempts=active_config.downloads.max_attempts,
            wake_workers=pool.notify,
        )
        library = LibraryAPI(
            queue,
            storage,
            active_config.telegram.authorized_users,
        )
        controller = TelegramBotController(
            active_clients.bot,
            jobs,
            library,
            queue,
            batch_identity=(active_config.sessions.path / "bot").as_posix(),
        )
        # Visible card recovery is best effort and per-session isolated. The
        # database rows are already restored, so a failed edit or send never
        # aborts startup, changes job state, or blocks other actors. Pending
        # terminal/retry results are restored before callback handlers start.
        restored_results = await controller.restore_pending_results(pending_results)
        if pending_results:
            logger.info(
                "Restart recovery delivered %d of %d pending results",
                restored_results,
                len(pending_results),
            )
        visible_cards = await controller.restore_interaction_cards(recovered_interactions)
        if recovered_interactions:
            logger.info(
                "Restart recovery confirmed %d of %d recovered cards visible",
                visible_cards,
                len(recovered_interactions),
            )
        controller.start()
        # The menu is optional: each scope is failure-isolated and startup
        # continues so typed commands and downloads stay available.
        await register_commands(active_clients.bot)
        await pool.start()
        for requester_id in active_config.telegram.authorized_users:
            try:
                paused_count = await jobs.paused_count(requester_id)
                await controller.send_startup_notice(requester_id, paused_count)
            except Exception as error:  # noqa: BLE001 - one chat cannot abort startup
                logger.warning(
                    "Startup notification failed: outcome=failed error_type=%s",
                    type(error).__name__,
                )
        logger.info("TGHarbor is running with %d worker(s)", pool.concurrency)
        await active_clients.bot.run_until_disconnected()
    finally:
        if batch_dispatcher is not None:
            await batch_dispatcher.stop()
        if controller is not None:
            controller.stop()
        if pool is not None:
            await pool.stop()
        if active_clients is not None:
            await active_clients.disconnect()
        lock.release()
