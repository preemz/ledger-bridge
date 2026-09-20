"""Regression tests for defects found by reading the code and by an audit pass.

Every one of these was a real defect that the existing suite happily passed over, which
is the reason each test states the failure mode rather than just the expected value.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.db.models import ProtectedError
from django.test import Client, override_settings

from apps.ledger.models import (
    Account,
    AuditEvent,
    JournalEntry,
    JournalLine,
    MigrationJob,
    MigrationPhase,
)
from apps.legacy_sim.models import LegacyJournalEntry
from apps.migration.models import StagedKind, StagedLegacyRow

CLIENT = Client()


# ---------------------------------------------------------------------------
# The dashboard number
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_stats_counts_the_ledger_not_the_sum_of_job_counters(seeded_legacy, in_process_legacy):
    """Two jobs over the same source system must not double the entries on the dashboard.

    Each job's counter is a ledger wide count, so summing them across jobs multiplied the
    figure by the number of jobs. Three jobs reported 12,471 entries against 4,157 in the
    table, on the first screen anybody looks at.
    """
    from apps.migration.runner import run_inline

    for reference in ("Stats job one", "Stats job two"):
        job = MigrationJob.objects.create(
            customer_reference=reference,
            source_system="legacy_erp",
            as_of_date=seeded_legacy.as_of_date,
        )
        run_inline(str(job.id))

    payload = CLIENT.get("/api/stats/").json()
    assert payload["jobs_total"] == 2
    assert payload["entries_loaded"] == JournalEntry.objects.count()
    assert payload["lines_loaded"] == JournalLine.objects.count()
    assert payload["entries_loaded"] == seeded_legacy.entry_count


# ---------------------------------------------------------------------------
# The operator timeline
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_a_superseded_attempt_is_marked_failed_not_left_running(seeded_legacy, in_process_legacy):
    """The killed attempt is a ghost: nothing will ever complete it.

    Leaving it RUNNING made the timeline claim a chunk was still in progress while the
    retry had already finished it.
    """
    from apps.migration import activities

    job = MigrationJob.objects.create(
        customer_reference="Ghost attempt",
        source_system="legacy_erp",
        as_of_date=seeded_legacy.as_of_date,
    )

    first = activities.extract_accounts(str(job.id))
    assert first["_phase"]["attempt"] == 1

    # Simulate the kill: the attempt that was in flight is left RUNNING because the
    # process died before it could record anything else.
    MigrationPhase.objects.filter(job=job, name="EXTRACT", attempt=1).update(
        status="RUNNING", finished_at=None
    )

    # Then the retry arrives under a new attempt number.
    original = activities._attempt
    activities._attempt = lambda: 2
    try:
        activities.extract_accounts(str(job.id))
    finally:
        activities._attempt = original

    rows = list(MigrationPhase.objects.filter(job=job, name="EXTRACT").order_by("attempt"))
    assert [row.attempt for row in rows] == [1, 2]
    assert rows[0].status == "FAILED"
    assert "superseded" in rows[0].error
    assert rows[0].finished_at is not None
    assert rows[1].status == "COMPLETED"
    # Exactly one row is left claiming to be in progress, and it is the completed one.
    assert MigrationPhase.objects.filter(job=job, status="RUNNING").count() == 0


@pytest.mark.django_db(transaction=True)
def test_the_label_distinguishes_two_kinds_of_work_under_one_phase_name(
    seeded_legacy, in_process_legacy
):
    """Accounts and entries both load under LOAD, and must not share a checkpoint."""
    from apps.migration import activities

    job = MigrationJob.objects.create(
        customer_reference="Label test",
        source_system="legacy_erp",
        as_of_date=seeded_legacy.as_of_date,
    )
    activities.extract_accounts(str(job.id))
    activities.load_accounts(str(job.id))
    activities.load_entries(str(job.id), 0, 100)

    load_rows = list(MigrationPhase.objects.filter(job=job, name="LOAD"))
    labels = {row.label for row in load_rows}
    assert "accounts" in labels
    assert any(label.startswith("entries") for label in labels)
    assert len(load_rows) == len({row.idempotency_key for row in load_rows})


# ---------------------------------------------------------------------------
# Data that would otherwise disappear
# ---------------------------------------------------------------------------


def test_an_entry_with_no_usable_date_is_rejected_outright():
    from apps.ledger.services.transform import normalise_entry

    normalised, problems = normalise_entry(
        {
            "id": "JE-NODATE",
            "date": "not a date",
            "lines": [
                {"id": "L1", "account_code": "1000", "debit": "10.00", "credit": "0"},
                {"id": "L2", "account_code": "4000", "debit": "0", "credit": "10.00"},
            ],
        }
    )
    # Returning a normalised, balanced entry here meant the entry counted as validated and
    # then vanished at load time, with no rejection count and no review.
    assert normalised is None
    assert any("date" in problem for problem in problems)


@pytest.mark.django_db(transaction=True)
def test_an_entry_whose_lines_have_no_source_ref_keeps_every_line():
    """The delete used to run per line, so each iteration wiped the previous one."""
    from apps.ledger.services.load import upsert_entries

    Account.objects.create(code="1000", name="Cash", type="ASSET")
    Account.objects.create(code="4000", name="Revenue", type="REVENUE")

    result = upsert_entries(
        [
            {
                "external_ref": "REF-LESS",
                "entry_date": dt.date(2026, 1, 31),
                "memo": "no line identifiers",
                "status": "POSTED",
                "lines": [
                    {"external_ref": "", "account_code": "1000", "debit": "100.00", "credit": "0"},
                    {"external_ref": "", "account_code": "4000", "debit": "0", "credit": "100.00"},
                ],
            }
        ],
        source_system="legacy_erp",
    )

    assert result.entries_upserted == 1, result.errors
    entry = JournalEntry.objects.get(external_ref="REF-LESS")
    assert entry.lines.count() == 2, "a ref-less line was deleted by the next iteration"
    assert sum(line.debit for line in entry.lines.all()) == Decimal("100.0000")


@pytest.mark.django_db(transaction=True)
def test_loading_the_same_ref_less_entry_twice_does_not_duplicate_its_lines():
    from apps.ledger.services.load import upsert_entries

    Account.objects.create(code="1000", name="Cash", type="ASSET")
    Account.objects.create(code="4000", name="Revenue", type="REVENUE")
    payload = [
        {
            "external_ref": "REF-LESS-2",
            "entry_date": dt.date(2026, 1, 31),
            "memo": "replayed",
            "status": "POSTED",
            "lines": [
                {"external_ref": "", "account_code": "1000", "debit": "50.00", "credit": "0"},
                {"external_ref": "", "account_code": "4000", "debit": "0", "credit": "50.00"},
            ],
        }
    ]

    upsert_entries(payload, source_system="legacy_erp")
    upsert_entries(payload, source_system="legacy_erp")

    assert JournalEntry.objects.count() == 1
    assert JournalLine.objects.count() == 2


@pytest.mark.django_db
def test_the_batch_counter_reports_a_single_batch_run():
    from apps.ledger.services.load import upsert_accounts

    rows = [
        {"id": f"A{index}", "code": f"{1000 + index}", "name": "Acc", "type": "ASSET"}
        for index in range(5)
    ]
    result = upsert_accounts(rows, source_system="legacy_erp")
    assert result.accounts_upserted == 5
    assert result.batches == 1, "a run smaller than one batch used to report zero batches"


# ---------------------------------------------------------------------------
# The audit trail's own integrity
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_a_job_with_an_audit_trail_cannot_be_deleted(seeded_legacy, in_process_legacy):
    """CASCADE produced a confusing database error naming a trigger.

    PROTECT refuses the delete for a clear reason: the audit trail and its subject are not
    things that quietly disappear.
    """
    from apps.migration.runner import run_inline

    job = MigrationJob.objects.create(
        customer_reference="Delete test",
        source_system="legacy_erp",
        as_of_date=seeded_legacy.as_of_date,
    )
    run_inline(str(job.id))
    assert AuditEvent.objects.filter(job=job).exists()

    with pytest.raises(ProtectedError):
        job.delete()

    assert MigrationJob.objects.filter(pk=job.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_the_reconciliation_audit_event_carries_the_classification_breakdown(
    seeded_legacy, in_process_legacy
):
    from apps.migration.runner import run_inline

    job = MigrationJob.objects.create(
        customer_reference="Audit payload test",
        source_system="legacy_erp",
        as_of_date=seeded_legacy.as_of_date,
    )
    run_inline(str(job.id))

    event = AuditEvent.objects.get(job=job, event_type="reconciliation.completed")
    assert event.payload["by_classification"], "the audit trail should be self contained"
    assert sum(event.payload["by_classification"].values()) == event.payload["breaks"]


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_staging_records_which_page_each_row_came_from(seeded_legacy, in_process_legacy):
    from apps.migration import activities

    job = MigrationJob.objects.create(
        customer_reference="Stage page test",
        source_system="legacy_erp",
        as_of_date=seeded_legacy.as_of_date,
    )
    with override_settings(MIGRATION_PAGE_SIZE=25):
        activities.extract_accounts(str(job.id))
        activities.stage_entries(str(job.id), 1, 100)

    source_count = LegacyJournalEntry.objects.count()
    expected_pages = (source_count + 24) // 25
    pages = set(
        StagedLegacyRow.objects.filter(job=job, kind=StagedKind.ENTRY)
        .order_by()
        .values_list("source_page", flat=True)
    )
    assert pages == set(range(1, expected_pages + 1))
    assert (
        StagedLegacyRow.objects.filter(job=job, kind=StagedKind.ENTRY).count() == source_count
    )
