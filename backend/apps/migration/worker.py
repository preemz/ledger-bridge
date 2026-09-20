"""The Temporal worker process.

Run one or more of these. Killing this process while a LOAD activity is in flight is
the crash-resume demonstration: Temporal notices the missed heartbeat, reschedules the
activity on the next available worker, and the upsert in the load service makes the
replay a no-op instead of a duplication.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from temporalio.client import Client
from temporalio.worker import Worker

from . import activities
from .workflows import LedgerMigrationWorkflow

logger = logging.getLogger(__name__)

# The activities in this module are synchronous, because they are Django service calls
# and there is nothing to gain from pretending otherwise. The Python SDK requires an
# executor for synchronous activities, and without one it refuses to start the worker
# with "is not async so an activity_executor must be present".
ACTIVITY_WORKERS = 8

ACTIVITIES = [
    activities.extract_accounts,
    activities.stage_entries,
    activities.transform_entries,
    activities.validate_entries,
    activities.load_accounts,
    activities.load_entries,
    activities.reconcile,
    activities.finalise,
]


async def run_worker() -> None:
    client = await Client.connect(settings.TEMPORAL_HOST, namespace=settings.TEMPORAL_NAMESPACE)
    # The executor must outlive the worker, so it is created here rather than inline.
    with ThreadPoolExecutor(max_workers=ACTIVITY_WORKERS) as activity_executor:
        worker = Worker(
            client,
            task_queue=settings.TEMPORAL_TASK_QUEUE,
            workflows=[LedgerMigrationWorkflow],
            activities=ACTIVITIES,
            activity_executor=activity_executor,
        )
        logger.info(
            "worker listening task_queue=%s temporal=%s namespace=%s activity_workers=%s",
            settings.TEMPORAL_TASK_QUEUE,
            settings.TEMPORAL_HOST,
            settings.TEMPORAL_NAMESPACE,
            ACTIVITY_WORKERS,
        )
        await worker.run()


def main() -> None:
    asyncio.run(run_worker())
