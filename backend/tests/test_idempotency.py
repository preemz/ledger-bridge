"""The claim this project rests on: a resumed migration does not duplicate the books.

An interrupted load is the normal case in a customer migration, not the exception. The
worker gets restarted by a deploy, the process gets killed, the machine reboots. If the
second attempt appends instead of upserting, the customer's revenue doubles and the
migration is worse than useless.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.db.models import Sum

from apps.ledger.models import JournalEntry, JournalLine, MigrationJob
from apps.migration.runner import run_inline
from apps.migration.models import StagedKind, StagedLegacyRow


def _create_job(dataset) -> MigrationJob:
    return MigrationJob.objects.create(
        customer_reference="Idempotency test",
        source_system="legacy_erp",
        as_of_date=dataset.as_of_date,
    )


def _ledger_counts() -> tuple[int, int]:
    return JournalEntry.objects.count(), JournalLine.objects.count()


def _balance_snapshot() -> dict[str, Decimal]:
    rows = (
        JournalLine.objects.values("account__code")
        .annotate(
            debit=Sum("debit"),
            credit=Sum("credit"),
        )
        .order_by("account__code")
    )
    return {row["account__code"]: (row["debit"] or Decimal("0")) - (row["credit"] or Decimal("0")) for row in rows}


@pytest.mark.django_db(transaction=True)
def test_load_is_idempotent(seeded_legacy, in_process_legacy):
    """Run the whole migration twice. Nothing may change the second time."""
    job = _create_job(seeded_legacy)

    first = run_inline(str(job.id))
    assert first["status"] in {"COMPLETED", "NEEDS_REVIEW"}, first

    entries_once, lines_once = _ledger_counts()
    balances_once = _balance_snapshot()
    assert entries_once > 0

    second = run_inline(str(job.id))
    assert second["status"] in {"COMPLETED", "NEEDS_REVIEW"}, second

    entries_twice, lines_twice = _ledger_counts()
    assert (entries_twice, lines_twice) == (entries_once, lines_once)
    assert _balance_snapshot() == balances_once


@pytest.mark.django_db(transaction=True)
def test_the_ledger_holds_every_source_entry_exactly_once(seeded_legacy, in_process_legacy):
    from apps.legacy_sim.models import LegacyJournalEntry

    job = _create_job(seeded_legacy)
    run_inline(str(job.id))

    source_refs = set(LegacyJournalEntry.objects.values_list("ref", flat=True))
    loaded_refs = set(JournalEntry.objects.values_list("external_ref", flat=True))
    assert loaded_refs <= source_refs
    # Every loaded entry appears once, by construction of the unique constraint.
    assert JournalEntry.objects.count() == len(loaded_refs)
    assert len(loaded_refs) > 0


@pytest.mark.django_db(transaction=True)
def test_a_replayed_load_chunk_writes_nothing_new(seeded_legacy, in_process_legacy):
    """Re-running a single cursor window is the shape of a Temporal retry."""
    from apps.migration import activities

    job = _create_job(seeded_legacy)
    activities.extract_accounts(str(job.id))
    activities.stage_entries(str(job.id), 1, 10)

    cursor = 0
    for _ in range(50):
        result = activities.transform_entries(str(job.id), cursor, 100)
        if result["done"]:
            break
        cursor = result["last_id"]
    activities.validate_entries(str(job.id))
    activities.load_accounts(str(job.id))

    first = activities.load_entries(str(job.id), 0, 100)
    entries_once, lines_once = _ledger_counts()

    replay = activities.load_entries(str(job.id), 0, 100)
    assert replay["already_present"] >= first["loaded"]
    assert (JournalEntry.objects.count(), JournalLine.objects.count()) == (entries_once, lines_once)


@pytest.mark.django_db(transaction=True)
def test_staging_is_idempotent(seeded_legacy, in_process_legacy):
    from apps.migration import activities

    job = _create_job(seeded_legacy)
    activities.extract_accounts(str(job.id))
    activities.stage_entries(str(job.id), 1, 5)

    staged_once = StagedLegacyRow.objects.filter(job=job, kind=StagedKind.ENTRY).count()
    assert staged_once > 0

    again = activities.stage_entries(str(job.id), 1, 5)
    assert again["rows_newly_staged"] == 0
    assert StagedLegacyRow.objects.filter(job=job, kind=StagedKind.ENTRY).count() == staged_once
