"""The audit log has to stay readable, or nobody reads it.

Phase successes are recorded in the phase timeline with their per chunk stats. Mirroring
every successful chunk into the audit log fills it with rows that describe work that
went right, which buries the rows that describe work that went wrong. A 4,000 entry
migration produced about twenty COMPLETED rows before this was changed.
"""

from __future__ import annotations

import pytest

from apps.ledger.models import AuditEvent, MigrationJob, MigrationPhase
from apps.migration.runner import run_inline


@pytest.mark.django_db(transaction=True)
def test_a_successful_migration_does_not_log_every_chunk(seeded_legacy, in_process_legacy):
    job = MigrationJob.objects.create(
        customer_reference="Audit noise test",
        source_system="legacy_erp",
        as_of_date=seeded_legacy.as_of_date,
    )
    run_inline(str(job.id))

    phases = MigrationPhase.objects.filter(job=job).count()
    completed_phase_events = AuditEvent.objects.filter(
        job=job, event_type="phase.completed"
    ).count()

    assert phases > 6, "this test only means something once phases are chunked"
    assert completed_phase_events == 0

    # The events that matter are still there.
    event_types = set(AuditEvent.objects.filter(job=job).values_list("event_type", flat=True))
    assert "job.created" in event_types
    assert "job.status_changed" in event_types
    assert "reconciliation.completed" in event_types
    assert "job.finished" in event_types


@pytest.mark.django_db(transaction=True)
def test_a_failed_phase_is_logged(seeded_legacy, in_process_legacy):
    job = MigrationJob.objects.create(
        customer_reference="Audit failure test",
        source_system="legacy_erp",
        as_of_date=seeded_legacy.as_of_date,
    )
    phase = MigrationPhase.objects.create(
        job=job,
        name="LOAD",
        status="FAILED",
        attempt=1,
        chunk=3,
        idempotency_key=f"{job.id}:LOAD:1:3:test",
        error="boom",
    )
    assert AuditEvent.objects.filter(job=job, event_type="phase.failed").count() == 1
    assert "boom" in AuditEvent.objects.filter(job=job, event_type="phase.failed").first().payload.get(
        "error", ""
    )
    assert phase.status == "FAILED"
