"""Validation has to fail for the right reason.

An earlier version of the validation phase checked entry account codes against the
ledger's Account table. Validation runs before the load, so that table was empty, every
line was reported as having an unknown account, and because that count feeds the
review decision every job was held for review with a false explanation attached.
"""

from __future__ import annotations

import pytest

from apps.ledger.models import MigrationJob
from apps.migration import activities


def _job(dataset) -> MigrationJob:
    return MigrationJob.objects.create(
        customer_reference="Validation test",
        source_system="legacy_erp",
        as_of_date=dataset.as_of_date,
    )


def _prepare(job) -> None:
    activities.extract_accounts(str(job.id))
    activities.stage_entries(str(job.id), 1, 20)
    cursor = 0
    for _ in range(50):
        result = activities.transform_entries(str(job.id), cursor, 500)
        if result["done"]:
            break
        cursor = result["last_id"]


@pytest.mark.django_db(transaction=True)
def test_a_clean_dataset_reports_no_unknown_accounts(seeded_legacy, in_process_legacy):
    job = _job(seeded_legacy)
    _prepare(job)
    stats = activities.validate_entries(str(job.id))

    assert stats["entries_checked"] > 0
    assert stats["chart_of_accounts_codes"] > 0
    assert stats["entries_rejected"] == 0
    assert stats["entries_unbalanced"] == 0
    assert stats["lines_with_unknown_account"] == 0, stats["unknown_accounts"]


@pytest.mark.django_db(transaction=True)
def test_an_entry_referencing_an_unknown_account_is_reported(seeded_legacy, in_process_legacy):
    from apps.migration.models import StagedKind, StagedLegacyRow

    job = _job(seeded_legacy)
    _prepare(job)

    # A journal line against an account that is not in the chart of accounts export.
    row = StagedLegacyRow.objects.filter(job=job, kind=StagedKind.ENTRY).first()
    payload = dict(row.normalised)
    payload["lines"] = [
        dict(payload["lines"][0], account_code="7777"),
        *payload["lines"][1:],
    ]
    row.normalised = payload
    row.save(update_fields=["normalised"])

    stats = activities.validate_entries(str(job.id))
    assert stats["lines_with_unknown_account"] >= 1
    assert "7777" in stats["unknown_accounts"]


@pytest.mark.django_db(transaction=True)
def test_an_unbalanced_entry_is_counted_and_not_loaded(seeded_legacy, in_process_legacy):
    from apps.migration.models import StagedKind, StagedLegacyRow

    job = _job(seeded_legacy)
    _prepare(job)

    row = StagedLegacyRow.objects.filter(job=job, kind=StagedKind.ENTRY).first()
    payload = dict(row.normalised)
    payload["balanced"] = False
    row.normalised = payload
    row.save(update_fields=["normalised"])

    stats = activities.validate_entries(str(job.id))
    assert stats["entries_unbalanced"] >= 1

    activities.load_accounts(str(job.id))
    result = activities.load_entries(str(job.id), 0, 500)
    assert result["skipped"] >= 1, "an unbalanced entry reached the load"
