"""Durable activities.

Everything here is a plain synchronous function so that the same code runs under the
Temporal worker and under the inline runner used in tests. That equality is
deliberate: a migration that only works when a workflow engine is present is a
migration you cannot test.

Three rules govern every activity in this module.

1. It may run more than once. Temporal guarantees at-least-once execution, not
   exactly-once. Every write is therefore an upsert or is guarded by an existing
   checkpoint.
2. Progress is recorded before it is reported. A counter that is only in memory is
   lost the moment the process is killed, and the operator console then lies.
3. It is chunked. A phase that takes four minutes is four minutes of lost work when
   the process dies; a phase split into twenty chunks loses one chunk.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import time
from typing import Any, Callable

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.ledger.models import (
    Account,
    EntryStatus,
    JournalEntry,
    JournalLine,
    MigrationJob,
    MigrationPhase,
    PhaseStatus,
)
from apps.ledger.services.legacy_client import LegacyApiClient
from apps.ledger.services.load import upsert_accounts, upsert_entries
from apps.ledger.services.reconcile import (
    refresh_balance_view,
    run_reconciliation,
    trial_balance_from_api,
)
from apps.ledger.services.transform import normalise_entry
from apps.ledger.services.util import audit, now

from .models import StagedKind, StagedLegacyRow

logger = logging.getLogger(__name__)

try:  # pragma: no cover - import guard for environments without the SDK
    from temporalio import activity
except Exception:  # noqa: BLE001
    activity = None  # type: ignore[assignment]


# Upper bound on chunks per phase. A migration that loops forever because a predicate
# never becomes false is worse than one that fails and tells you why.
MAX_CHUNKS = 500


def _job(job_id: str) -> MigrationJob:
    return MigrationJob.objects.get(pk=job_id)


def _attempt() -> int:
    """Temporal's attempt number, or 1 when running outside a worker."""
    if activity is None:
        return 1
    try:
        return int(activity.info().attempt)
    except Exception:  # noqa: BLE001 - outside an activity context
        return 1


def _heartbeat(detail: str, **extra: Any) -> None:
    if activity is None:
        return
    try:
        activity.heartbeat({"detail": detail, **extra})
    except Exception:  # noqa: BLE001 - heartbeat needs a running activity
        pass


def _pacing_seconds() -> float:
    """Optional artificial delay per batch.

    Used only by the demo runbook: it widens the window during which the worker can be
    killed mid-load so the crash and resume are visible on camera. Defaults to zero and
    has no effect on any real run.
    """
    try:
        return max(0.0, int(os.environ.get("MIGRATION_PACING_MS", "0")) / 1000.0)
    except ValueError:
        return 0.0


def run_phase(
    job: MigrationJob,
    name: str,
    work: Callable[[], dict[str, Any]],
    chunk: int = 0,
    label: str = "",
) -> dict[str, Any]:
    """Execute one phase chunk and record it as a durable checkpoint."""
    attempt = _attempt()
    # The label keeps two different kinds of work under the same phase name (accounts
    # and entries both live under LOAD) from resolving to the same checkpoint row.
    key = f"{job.id}:{name}:{attempt}:{chunk}:{label}"
    row, created = MigrationPhase.objects.get_or_create(
        idempotency_key=key,
        defaults={
            "job": job,
            "name": name,
            "attempt": attempt,
            "chunk": chunk,
            "label": label,
            "status": PhaseStatus.RUNNING,
            "started_at": now(),
        },
    )

    # An earlier attempt that never finished is a ghost. The process that was running it
    # was killed, so nothing will ever complete that row, and leaving it RUNNING makes the
    # operator timeline claim work is still in progress for a chunk the retry has already
    # finished. Marking it failed is the honest record of what happened.
    MigrationPhase.objects.filter(
        job=job, name=name, chunk=chunk, label=label, status=PhaseStatus.RUNNING
    ).exclude(pk=row.pk).update(
        status=PhaseStatus.FAILED,
        finished_at=now(),
        error="superseded by a retry after the worker stopped mid-phase",
    )
    if not created:
        row.status = PhaseStatus.RUNNING
        row.started_at = row.started_at or now()
        row.finished_at = None
        row.error = ""
        row.save(update_fields=["status", "started_at", "finished_at", "error", "updated_at"])

    job.refresh_from_db(fields=["status", "counters"])
    if job.status not in {"RUNNING", "PAUSED"}:
        job.status = "RUNNING"
        job.started_at = job.started_at or now()
        job.save(update_fields=["status", "started_at", "updated_at"])

    started = time.perf_counter()
    try:
        stats = work() or {}
    except Exception as exc:
        row.status = PhaseStatus.FAILED
        row.error = f"{exc.__class__.__name__}: {exc}"[:2000]
        row.finished_at = now()
        row.save(update_fields=["status", "error", "finished_at", "updated_at"])
        logger.exception("phase %s failed (attempt %s chunk %s)", name, attempt, chunk)
        raise

    stats = dict(stats)
    stats["duration_ms"] = int((time.perf_counter() - started) * 1000)
    row.status = PhaseStatus.COMPLETED
    row.stats = stats
    row.finished_at = now()
    row.save(update_fields=["status", "stats", "finished_at", "updated_at"])
    _heartbeat(f"{name} chunk {chunk} complete", **stats)

    # Returned unwrapped, not nested under the phase record. The workflow and the inline
    # runner read keys like done, last_id and unresolved_high straight off the result, and
    # burying those one level down is how a control loop silently stops terminating.
    return dict(
        stats,
        _phase={"name": name, "chunk": chunk, "attempt": attempt, "status": PhaseStatus.COMPLETED},
    )


# ---------------------------------------------------------------------------
# EXTRACT: pull the chart of accounts out of the legacy ERP
# ---------------------------------------------------------------------------


@activity.defn if activity else (lambda fn: fn)
def extract_accounts(job_id: str) -> dict[str, Any]:
    """Read the legacy chart of accounts and stage it, page by page."""
    job = _job(job_id)

    def work() -> dict[str, Any]:
        client = LegacyApiClient()
        page = 1
        total_pages = 1
        staged = 0
        seen = 0
        while page <= total_pages and page <= MAX_CHUNKS:
            result = client.accounts(page=page)
            total_pages = result.total_pages
            seen += len(result.items)
            before = StagedLegacyRow.objects.filter(job=job, kind=StagedKind.ACCOUNT).count()
            StagedLegacyRow.objects.bulk_create(
                [
                    StagedLegacyRow(
                        job=job,
                        kind=StagedKind.ACCOUNT,
                        external_ref=str(item.get("id") or "").strip(),
                        source_page=page,
                        payload=item,
                    )
                    for item in result.items
                    if str(item.get("id") or "").strip()
                ],
                ignore_conflicts=True,
            )
            staged += (
                StagedLegacyRow.objects.filter(job=job, kind=StagedKind.ACCOUNT).count() - before
            )
            _heartbeat("accounts staged", page=page, total_pages=total_pages)
            page += 1

        # Probing the journal endpoint here means the workflow knows how many entry
        # chunks to schedule without the accounts activity having to interpret them.
        entries_page = client.journal_entries(page=1, page_size=1)
        job.set_counters(accounts_seen=seen, accounts_staged=staged)
        return {
            "accounts_seen": seen,
            "accounts_newly_staged": staged,
            "entry_total_items": entries_page.total_items,
            "retry_stats": client.stats.as_dict(),
        }

    return run_phase(job, "EXTRACT", work)


# ---------------------------------------------------------------------------
# STAGE: pull the journal, page chunks, idempotent
# ---------------------------------------------------------------------------


@activity.defn if activity else (lambda fn: fn)
def stage_entries(job_id: str, start_page: int, page_count: int) -> dict[str, Any]:
    """Stage a window of journal pages.

    Re-running a window that already landed creates nothing, because the staging
    table is unique on (job, kind, external_ref) and the insert ignores conflicts.
    This is what makes an interrupted extract cheap to resume.
    """
    job = _job(job_id)

    def work() -> dict[str, Any]:
        client = LegacyApiClient()
        page = start_page
        window_end = start_page + page_count - 1
        # Unknown until the first response comes back, so initialised to the start page
        # to guarantee one iteration.
        total_pages = start_page
        seen = 0
        staged = 0
        # What was actually staged, which is the only honest source for the next page.
        # Deriving next_page from the loop variable skips the page at every window
        # boundary: with the default page size that is 500 journal entries per window,
        # and the only symptom is a reconciliation report that quietly disagrees with the
        # source system. That is precisely the class of bug the reconciliation exists to
        # catch, so it must not live in the code under it.
        last_staged = start_page - 1

        while page <= window_end and page <= total_pages:
            result = client.journal_entries(page=page)
            total_pages = result.total_pages
            if not result.items:
                break
            seen += len(result.items)
            before = StagedLegacyRow.objects.filter(job=job, kind=StagedKind.ENTRY).count()
            StagedLegacyRow.objects.bulk_create(
                [
                    StagedLegacyRow(
                        job=job,
                        kind=StagedKind.ENTRY,
                        external_ref=str(item.get("id") or "").strip(),
                        source_page=page,
                        payload=item,
                    )
                    for item in result.items
                    if str(item.get("id") or "").strip()
                ],
                ignore_conflicts=True,
            )
            staged += StagedLegacyRow.objects.filter(job=job, kind=StagedKind.ENTRY).count() - before
            last_staged = page
            _heartbeat("entries staged", page=page, total_pages=total_pages)
            page += 1
            if _pacing_seconds():
                time.sleep(_pacing_seconds())

        job.bump(rows_read=seen, rows_staged=staged)
        return {
            "start_page": start_page,
            "last_page_staged": last_staged,
            "total_pages": total_pages,
            "rows_seen": seen,
            "rows_newly_staged": staged,
            "done": last_staged >= total_pages,
            "next_page": last_staged + 1,
            "retry_stats": client.stats.as_dict(),
        }

    return run_phase(job, "STAGE", work, chunk=start_page, label=f"p{start_page}")


# ---------------------------------------------------------------------------
# TRANSFORM: normalise staged entries into ledger shape
# ---------------------------------------------------------------------------


@activity.defn if activity else (lambda fn: fn)
def transform_entries(job_id: str, after_id: int, limit: int) -> dict[str, Any]:
    """Normalise a cursor window of staged entries. Cursor based, not offset based.

    An offset would skip or repeat rows the moment the underlying set changes size.
    A cursor on the primary key cannot.
    """
    job = _job(job_id)

    def work() -> dict[str, Any]:
        rows = list(
            StagedLegacyRow.objects.filter(
                job=job, kind=StagedKind.ENTRY, normalised__isnull=True, id__gt=after_id
            ).order_by("id")[:limit]
        )
        if not rows:
            return {"transformed": 0, "done": True, "last_id": after_id, "problems": 0}

        problems: list[str] = []
        balanced = 0
        unbalanced = 0
        for row in rows:
            normalised, row_problems = normalise_entry(row.payload)
            if normalised is None:
                # Record the refusal on the row rather than dropping it, so the failure
                # is visible in the report instead of silently reducing the row count.
                row.normalised = {"__rejected__": True, "problems": row_problems}
                problems.extend(f"{row.external_ref}: {p}" for p in row_problems)
            else:
                row.normalised = normalised
                if normalised.get("balanced"):
                    balanced += 1
                else:
                    unbalanced += 1
                    problems.extend(f"{row.external_ref}: {p}" for p in row_problems)
        with transaction.atomic():
            StagedLegacyRow.objects.bulk_update(rows, ["normalised"])

        job.bump(entries_transformed=len(rows), entries_unbalanced=unbalanced)
        last_id = rows[-1].id
        remaining = StagedLegacyRow.objects.filter(
            job=job, kind=StagedKind.ENTRY, normalised__isnull=True, id__gt=last_id
        ).count()
        if problems:
            audit(
                job,
                "transform.problems",
                f"{len(problems)} problems in window after id {after_id}",
                sample=problems[:10],
            )
        return {
            "transformed": len(rows),
            "balanced": balanced,
            "unbalanced": unbalanced,
            "problems": len(problems),
            "last_id": last_id,
            "remaining": remaining,
            "done": remaining == 0,
        }

    return run_phase(job, "TRANSFORM", work, chunk=after_id, label=f"after{after_id}")


# ---------------------------------------------------------------------------
# VALIDATE: refuse to load books that do not add up
# ---------------------------------------------------------------------------


@activity.defn if activity else (lambda fn: fn)
def validate_entries(job_id: str) -> dict[str, Any]:
    """Check the staged, normalised entries before anything is written to the ledger.

    This is the gate a finance team cares about. An entry that does not balance must
    never reach the ledger, and an entry that references an unknown account must be
    reported with the account code that is missing so somebody can fix the mapping.
    """
    job = _job(job_id)

    def work() -> dict[str, Any]:
        # The chart of accounts to validate against is the one the source system
        # exported, read from staging, not the ledger table. Validation runs before the
        # load, so the ledger's Account table is empty at this point: checking against it
        # reported every single line as having an unknown account, and because that count
        # feeds the review decision, every job was held for review for the wrong reason.
        # Accounts already in the ledger are included so that a re-run of an earlier job
        # does not regress to reporting valid codes as unknown.
        known_codes = set(Account.objects.values_list("code", flat=True))
        staged_codes = 0
        for row in StagedLegacyRow.objects.filter(job=job, kind=StagedKind.ACCOUNT).iterator(
            chunk_size=500
        ):
            code = str((row.payload or {}).get("code") or "").strip()
            if code:
                known_codes.add(code)
                staged_codes += 1

        rejected = 0
        unbalanced = 0
        unknown_account = 0
        unknown_codes: dict[str, int] = {}
        checked = 0

        for row in StagedLegacyRow.objects.filter(job=job, kind=StagedKind.ENTRY).iterator(
            chunk_size=500
        ):
            payload = row.normalised or {}
            if payload.get("__rejected__"):
                rejected += 1
                continue
            checked += 1
            if not payload.get("balanced", False):
                unbalanced += 1
            for line in payload.get("lines") or []:
                code = line.get("account_code")
                if code and code not in known_codes:
                    unknown_account += 1
                    unknown_codes[code] = unknown_codes.get(code, 0) + 1

        stats = {
            "entries_checked": checked,
            "entries_rejected": rejected,
            "entries_unbalanced": unbalanced,
            "chart_of_accounts_codes": staged_codes,
            "lines_with_unknown_account": unknown_account,
            "unknown_accounts": dict(sorted(unknown_codes.items(), key=lambda kv: -kv[1])[:20]),
        }
        job.set_counters(
            entries_validated=checked,
            entries_rejected=rejected,
            entries_unbalanced=unbalanced,
        )
        if unknown_account:
            audit(
                job,
                "validate.unknown_accounts",
                f"{len(unknown_codes)} account codes are missing from the chart of accounts",
                codes=stats["unknown_accounts"],
            )
        return stats

    return run_phase(job, "VALIDATE", work)


# ---------------------------------------------------------------------------
# LOAD: the idempotent write
# ---------------------------------------------------------------------------


@activity.defn if activity else (lambda fn: fn)
def load_entries(job_id: str, after_id: int, limit: int) -> dict[str, Any]:
    """Upsert a cursor window of entries into the ledger.

    This is the activity the demo kills mid-flight. When it is retried, the entries it
    already wrote are found by (source_system, external_ref) and left alone, so the
    ledger ends up with the source row count and not a multiple of it.
    """
    job = _job(job_id)

    def work() -> dict[str, Any]:
        rows = list(
            StagedLegacyRow.objects.filter(
                job=job,
                kind=StagedKind.ENTRY,
                normalised__isnull=False,
                id__gt=after_id,
            ).order_by("id")[:limit]
        )
        if not rows:
            return {"loaded": 0, "done": True, "last_id": after_id}

        entries = []
        skipped = 0
        for row in rows:
            payload = row.normalised or {}
            if payload.get("__rejected__") or not payload.get("balanced", False):
                skipped += 1
                continue
            entry_date = payload.get("entry_date")
            if not entry_date:
                skipped += 1
                continue
            entries.append(
                {
                    "external_ref": payload["external_ref"],
                    "entry_date": dt.date.fromisoformat(entry_date),
                    "memo": payload.get("memo", ""),
                    "status": payload.get("status", EntryStatus.POSTED),
                    "currency": payload.get("currency", "USD"),
                    "lines": payload.get("lines") or [],
                }
            )

        result = upsert_entries(entries, source_system=job.source_system)
        last_id = rows[-1].id
        remaining = StagedLegacyRow.objects.filter(
            job=job, kind=StagedKind.ENTRY, normalised__isnull=False, id__gt=last_id
        ).count()

        # Read the progress counters off the ledger rather than accumulating them from
        # what this execution wrote. An activity that is retried after a crash only writes
        # the rows the previous attempt did not reach, so an accumulator under-reports
        # exactly when the operator most wants to know where things stand.
        job.bump(rows_skipped=skipped + result.entries_skipped)
        in_ledger = JournalEntry.objects.filter(source_system=job.source_system).count()
        job.set_counters(
            rows_loaded=in_ledger,
            entries_loaded=in_ledger,
            lines_loaded=JournalLine.objects.filter(
                entry__source_system=job.source_system
            ).count(),
        )
        _heartbeat("loaded batch", last_id=last_id, upserted=result.entries_upserted)

        if _pacing_seconds():
            time.sleep(_pacing_seconds())

        return {
            "loaded": result.entries_upserted,
            "already_present": result.entries_skipped,
            "lines_upserted": result.lines_upserted,
            "skipped": skipped,
            "errors": result.errors[:10],
            "last_id": last_id,
            "remaining": remaining,
            "done": remaining == 0,
            "upsert": result.as_dict(),
        }

    return run_phase(job, "LOAD", work, chunk=after_id, label=f"entries{after_id}")


@activity.defn if activity else (lambda fn: fn)
def load_accounts(job_id: str) -> dict[str, Any]:
    """Upsert the staged chart of accounts before any entry references it."""
    job = _job(job_id)

    def work() -> dict[str, Any]:
        payloads = [
            row.payload
            for row in StagedLegacyRow.objects.filter(job=job, kind=StagedKind.ACCOUNT).order_by("id")
        ]
        result = upsert_accounts(payloads, source_system=job.source_system)
        # Counted from the table, not from this execution, for the same reason as the
        # entry counters: a retried activity only writes what the previous attempt missed.
        job.set_counters(accounts_loaded=Account.objects.count())
        return dict(result.as_dict(), accounts_in_ledger=Account.objects.count())

    return run_phase(job, "LOAD", work, chunk=0, label="accounts")


# ---------------------------------------------------------------------------
# RECONCILE
# ---------------------------------------------------------------------------


@activity.defn if activity else (lambda fn: fn)
def reconcile(job_id: str) -> dict[str, Any]:
    job = _job(job_id)

    def work() -> dict[str, Any]:
        legacy = trial_balance_from_api(as_of=job.as_of_date)
        run = run_reconciliation(job, legacy)
        refresh_balance_view()
        job.set_counters(
            breaks_found=run.breaks.count(),
            reconciliation_runs=(job.counters or {}).get("reconciliation_runs", 0) + 1,
        )
        unresolved_high = run.breaks.filter(resolved=False, severity="HIGH").count()
        return {
            "accounts_compared": len(legacy.codes),
            "legacy_entries": legacy.entry_count,
            "loaded_entries": run.loaded_entry_count,
            "breaks": run.breaks.count(),
            "unresolved_high": unresolved_high,
            "legacy_total_debit": str(run.legacy_total_debit),
            "loaded_total_debit": str(run.loaded_total_debit),
            "difference": str(run.difference),
            "duration_ms": run.duration_ms,
        }

    return run_phase(job, "RECONCILE", work)


@activity.defn if activity else (lambda fn: fn)
def finalise(job_id: str, status: str, error: str = "") -> dict[str, Any]:
    """Set the terminal status once the workflow has decided what it is.

    Deliberately not wrapped in run_phase: finalising is not a phase, and giving it a
    phase row would put a seventh entry in the operator timeline that means nothing to
    the customer.
    """
    job = _job(job_id)
    job.status = status
    job.error = error[:4000]
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "error", "finished_at", "updated_at"])
    audit(job, "job.finished", f"migration finished with status {status}")
    return {"status": status}


# Order of execution, so the inline runner and the workflow cannot drift apart.
PHASE_SEQUENCE = [
    "extract_accounts",
    "stage_entries",
    "transform_entries",
    "validate_entries",
    "load_accounts",
    "load_entries",
    "reconcile",
]