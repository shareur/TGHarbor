from __future__ import annotations

import logging

from tgharbor.app.instance_lock import InstanceLock
from tgharbor.config import AppConfig, load_config
from tgharbor.core.job_api import JobAPI
from tgharbor.core.jobs import SourceSession
from tgharbor.core.library_api import LibraryAPI, recover_pending_organizations
from tgharbor.core.reconciliation import LegacyReconciler
from tgharbor.core.worker import DownloadWorker, WorkerPool, WorkerSettings
from tgharbor.persistence import SQLiteJobQueue
from tgharbor.storage import LocalStorage
from tgharbor.telegram.bot import TelegramBotController, TelegramJobNotifier
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
    active_clients = clients
    lock.acquire()
    try:
        queue = SQLiteJobQueue(active_config.database.path)
        await queue.initialize()
        recovered, exhausted = await queue.recover_interrupted()
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
        if active_clients is None:
            active_clients = build_clients(active_config)
        await start_service_clients(active_config, active_clients)

        notifier = TelegramJobNotifier(active_clients.bot, queue)
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
        controller = TelegramBotController(active_clients.bot, jobs, library, queue)
        controller.start()
        await pool.start()
        logger.info("TGHarbor is running with %d worker(s)", pool.concurrency)
        await active_clients.bot.run_until_disconnected()
    finally:
        if controller is not None:
            controller.stop()
        if pool is not None:
            await pool.stop()
        if active_clients is not None:
            await active_clients.disconnect()
        lock.release()
