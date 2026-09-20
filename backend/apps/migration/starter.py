"""Starting a migration.

The API asks for a migration; this module decides who runs it. Temporal is the real
answer. The inline runner is the fallback for a machine without a Temporal server, so
that the API never leaves a job sitting in PENDING with no explanation.

Because the workflow id is derived from the job id, asking twice for the same job does
not start two migrations. Temporal rejects the duplicate, which is the behaviour you
want when an operator double clicks a button.
"""

from __future__ import annotations

import asyncio
import logging

from django.conf import settings

from apps.ledger.models import MigrationJob
from apps.ledger.services.util import audit

from .plan import build_config
from .runner import run_inline_in_thread

logger = logging.getLogger(__name__)


def workflow_id_for(job: MigrationJob) -> str:
    return f"ledger-migration-{job.id}"


def config_for_workflow() -> dict:
    return build_config(
        activity_timeout_seconds=settings.TEMPORAL_ACTIVITY_TIMEOUT_SECONDS,
        heartbeat_seconds=settings.TEMPORAL_HEARTBEAT_SECONDS,
        batch_size=settings.MIGRATION_BATCH_SIZE,
    )


async def _start_on_temporal(job: MigrationJob) -> tuple[str, str]:
    from temporalio.client import Client

    from .workflows import LedgerMigrationWorkflow

    client = await Client.connect(settings.TEMPORAL_HOST, namespace=settings.TEMPORAL_NAMESPACE)
    handle = await client.start_workflow(
        LedgerMigrationWorkflow.run,
        args=[str(job.id), config_for_workflow()],
        id=workflow_id_for(job),
        task_queue=settings.TEMPORAL_TASK_QUEUE,
    )
    return handle.id, (handle.result_run_id or "")


def start_migration(job: MigrationJob) -> dict:
    """Start the migration and return how it was started."""
    if settings.TEMPORAL_ENABLED:
        try:
            workflow_id, run_id = asyncio.run(_start_on_temporal(job))
            job.temporal_workflow_id = workflow_id
            job.temporal_run_id = run_id
            job.executor = "temporal"
            job.save(update_fields=["temporal_workflow_id", "temporal_run_id", "executor", "updated_at"])
            audit(job, "job.started", "workflow started on Temporal", workflow_id=workflow_id)
            return {"executor": "temporal", "workflow_id": workflow_id, "run_id": run_id}
        except Exception as exc:  # noqa: BLE001 - fall back rather than strand the job
            logger.warning("temporal unavailable, falling back to inline runner: %s", exc)
            audit(
                job,
                "job.fallback",
                "Temporal was unreachable, running inline instead",
                error=str(exc)[:300],
            )

    thread = run_inline_in_thread(str(job.id))
    job.executor = "inline"
    job.status = "RUNNING"
    job.save(update_fields=["executor", "status", "updated_at"])
    audit(job, "job.started", "migration started on the inline runner")
    return {"executor": "inline", "thread": thread.name}


async def _signal_on_temporal(job: MigrationJob, signal: str) -> str:
    from temporalio.client import Client

    client = await Client.connect(settings.TEMPORAL_HOST, namespace=settings.TEMPORAL_NAMESPACE)
    handle = client.get_workflow_handle(workflow_id_for(job))
    if signal == "pause":
        await handle.signal("pause")
        return "PAUSED"
    if signal == "resume":
        await handle.signal("resume")
        return "RUNNING"
    if signal == "cancel":
        await handle.signal("cancel")
        return "FAILED"
    raise ValueError(f"unknown signal {signal!r}")


def send_signal(job: MigrationJob, signal: str) -> dict:
    """Pause, resume or cancel. Returns the status the console should show.

    Pause is honoured between activity chunks, so the job shows PAUSED immediately while
    the in-flight chunk finishes. That is stated to the operator in the UI rather than
    hidden, because a pause button that appears to do nothing is worse than one that
    explains itself.
    """
    if job.executor != "temporal" or not settings.TEMPORAL_ENABLED:
        audit(job, "job.signal_rejected", f"{signal} is not available on the inline runner")
        return {
            "ok": False,
            "detail": "This migration is running on the inline runner, which cannot be signalled. "
            "Set TEMPORAL_ENABLED=true and run the worker to use pause, resume and cancel.",
        }

    try:
        status = asyncio.run(_signal_on_temporal(job, signal))
    except Exception as exc:  # noqa: BLE001
        logger.warning("signal %s failed for job %s: %s", signal, job.id, exc)
        audit(job, "job.signal_failed", f"{signal} failed", error=str(exc)[:300])
        return {"ok": False, "detail": f"Signal failed: {exc}"}

    job.status = status
    job.save(update_fields=["status", "updated_at"])
    audit(job, "job.signal", f"{signal} delivered", status=status)
    return {"ok": True, "signal": signal, "status": status}
