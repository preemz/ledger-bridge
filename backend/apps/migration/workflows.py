"""The migration workflow.

This is the durable execution plan. Read it as the answer to "what happens if the
process dies here?" at every line.

* Every step is an activity, so the workflow itself is just a state machine. Temporal
  records the outcome of each completed activity in the event history, which is why a
  worker restart resumes at the failed step rather than at the beginning.
* Long phases are loops of chunk activities. A crash costs one chunk.
* ``pause`` and ``cancel`` are signals. Pause takes effect between chunks: it will not
  interrupt an activity that is already running, because interrupting a database write
  half way through is exactly what the idempotency work exists to avoid.
* The workflow decides the terminal status, because it is the only thing that can see
  the whole run. It never touches the database itself.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

from .plan import MAX_CHUNKS

with workflow.unsafe.imports_passed_through():
    from . import activities as act

# Five attempts is deliberate: three is often not enough to ride out a rate limited
# window on the customer's side, and more than five usually means the failure is not
# transient and waiting longer just delays the error.
RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=5,
)


@workflow.defn
class LedgerMigrationWorkflow:
    def __init__(self) -> None:
        self._paused = False
        self._cancelled = False
        self._current = "starting"
        self._completed: list[str] = []

    # -- signals -----------------------------------------------------------

    @workflow.signal
    def pause(self) -> None:
        self._paused = True

    @workflow.signal
    def resume(self) -> None:
        self._paused = False

    @workflow.signal
    def cancel(self) -> None:
        self._cancelled = True

    @workflow.query
    def status(self) -> dict[str, Any]:
        return {
            "paused": self._paused,
            "cancelled": self._cancelled,
            "current": self._current,
            "completed": list(self._completed),
        }

    # -- helpers -----------------------------------------------------------

    async def _checkpoint(self, stage: str) -> None:
        """Gate between chunks: honour a pause or a cancel, then record position."""
        self._current = stage
        if self._paused:
            await workflow.wait_condition(lambda: not self._paused or self._cancelled)
        if self._cancelled:
            raise RuntimeError("cancelled by operator")

    def _options(self, config: dict[str, Any]) -> dict[str, Any]:
        return {
            "start_to_close_timeout": timedelta(seconds=config["activity_timeout_seconds"]),
            "heartbeat_timeout": timedelta(seconds=config["heartbeat_seconds"]),
            "retry_policy": RETRY_POLICY,
        }

    # -- run ---------------------------------------------------------------

    @workflow.run
    async def run(self, job_id: str, config: dict[str, Any]) -> dict[str, Any]:
        options = self._options(config)
        batch = int(config["batch_size"])
        pages_per_stage = int(config["pages_per_stage_activity"])
        max_chunks = int(config.get("max_chunks", MAX_CHUNKS))

        try:
            await self._checkpoint("extract")
            extract = await workflow.execute_activity(act.extract_accounts, job_id, **options)
            self._completed.append("EXTRACT")

            await self._checkpoint("stage")
            page = 1
            staged_pages = 0
            while True:
                result = await workflow.execute_activity(
                    act.stage_entries, args=[job_id, page, pages_per_stage], **options
                )
                staged_pages += 1
                if result.get("done") or staged_pages >= max_chunks:
                    break
                page = int(result.get("next_page", page + pages_per_stage))
            self._completed.append("STAGE")

            await self._checkpoint("transform")
            cursor = 0
            chunks = 0
            while True:
                result = await workflow.execute_activity(
                    act.transform_entries, args=[job_id, cursor, batch], **options
                )
                chunks += 1
                if result.get("done") or chunks >= max_chunks:
                    break
                cursor = int(result["last_id"])
            self._completed.append("TRANSFORM")

            await self._checkpoint("validate")
            validation = await workflow.execute_activity(act.validate_entries, job_id, **options)
            self._completed.append("VALIDATE")

            await self._checkpoint("load")
            await workflow.execute_activity(act.load_accounts, job_id, **options)
            cursor = 0
            chunks = 0
            while True:
                result = await workflow.execute_activity(
                    act.load_entries, args=[job_id, cursor, batch], **options
                )
                chunks += 1
                if result.get("done") or chunks >= max_chunks:
                    break
                cursor = int(result["last_id"])
            self._completed.append("LOAD")

            await self._checkpoint("reconcile")
            reconciliation = await workflow.execute_activity(act.reconcile, job_id, **options)
            self._completed.append("RECONCILE")

        except Exception as exc:  # noqa: BLE001 - any failure ends the run as FAILED
            await workflow.execute_activity(
                act.finalise,
                args=[job_id, "FAILED", f"{exc.__class__.__name__}: {exc}"],
                **options,
            )
            raise

        # A migration that completed its phases can still be wrong. Anything the
        # validation pass rejected, and any unexplained high severity break, means a
        # human has to look before the customer is told the books agree.
        needs_review = bool(
            validation.get("entries_rejected")
            or validation.get("lines_with_unknown_account")
            or reconciliation.get("unresolved_high")
        )
        status = "NEEDS_REVIEW" if needs_review else "COMPLETED"

        await workflow.execute_activity(act.finalise, args=[job_id, status, ""], **options)

        return {
            "job_id": job_id,
            "status": status,
            "extract": extract,
            "validation": validation,
            "reconciliation": reconciliation,
        }
