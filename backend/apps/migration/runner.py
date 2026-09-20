"""Inline runner: the same plan, executed in a background thread.

Two reasons this exists rather than Temporal being mandatory.

1. Tests. A migration that can only be exercised with a workflow server running is a
   migration whose failure paths are never tested.
2. A reviewer on a machine without the Temporal CLI can still watch a migration run.

It calls the activity functions directly and in the same order as the workflow. The
chunk sizes come from the shared plan module, so the two paths cannot disagree about
how the work is divided.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from django.conf import settings
from django.db import close_old_connections

from apps.ledger.models import MigrationJob

from . import activities
from .plan import BATCH_SIZE, MAX_CHUNKS, STAGE_PAGES_PER_ACTIVITY

logger = logging.getLogger(__name__)


def run_inline(job_id: str) -> dict[str, Any]:
    """Execute every phase for a job in the current thread."""
    batch = getattr(settings, "MIGRATION_BATCH_SIZE", BATCH_SIZE)

    def run() -> dict[str, Any]:
        try:
            extract = activities.extract_accounts(job_id)

            page = 1
            chunks = 0
            while chunks < MAX_CHUNKS:
                result = activities.stage_entries(job_id, page, STAGE_PAGES_PER_ACTIVITY)
                chunks += 1
                if result.get("done"):
                    break
                page = int(result.get("next_page", page + STAGE_PAGES_PER_ACTIVITY))

            cursor = 0
            chunks = 0
            while chunks < MAX_CHUNKS:
                result = activities.transform_entries(job_id, cursor, batch)
                chunks += 1
                if result.get("done"):
                    break
                cursor = int(result["last_id"])

            validation = activities.validate_entries(job_id)

            activities.load_accounts(job_id)

            cursor = 0
            chunks = 0
            while chunks < MAX_CHUNKS:
                result = activities.load_entries(job_id, cursor, batch)
                chunks += 1
                if result.get("done"):
                    break
                cursor = int(result["last_id"])

            reconciliation = activities.reconcile(job_id)

            needs_review = bool(
                validation.get("entries_rejected")
                or validation.get("lines_with_unknown_account")
                or reconciliation.get("unresolved_high")
            )
            status = "NEEDS_REVIEW" if needs_review else "COMPLETED"
            activities.finalise(job_id, status, "")

            return {
                "job_id": job_id,
                "status": status,
                "extract": extract,
                "validation": validation,
                "reconciliation": reconciliation,
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("inline migration failed for job %s", job_id)
            activities.finalise(job_id, "FAILED", f"{exc.__class__.__name__}: {exc}")
            return {"job_id": job_id, "status": "FAILED", "error": str(exc)}

    logger.info("running migration %s inline", job_id)
    return run()


def run_inline_in_thread(job_id: str) -> threading.Thread:
    """Fire and forget, on a daemon thread, with its own database connection."""

    def target() -> None:
        close_old_connections()
        try:
            run_inline(job_id)
        finally:
            close_old_connections()

    thread = threading.Thread(target=target, name=f"migration-{job_id}", daemon=True)
    thread.start()
    return thread
