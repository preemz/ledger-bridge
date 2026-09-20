"""Multi window pagination, against the real staging activity.

The bug this guards against: the staging loop reported its next page from the loop
variable, which finishes one past the window it just processed. Every window therefore
skipped the page at its boundary, which with the default page size is 500 journal
entries lost per window. Nothing failed. The reconciliation simply disagreed with the
source system by a small, plausible amount, which is the worst possible failure mode for
a tool whose entire job is noticing disagreements.

The first version of this test re-implemented the loop and asserted the arithmetic. That
proves nothing about the code that ships, so this version drives the real activity with a
small page size, forces several windows, and then checks the database.
"""

from __future__ import annotations

import pytest
from django.test import override_settings

from apps.ledger.models import MigrationJob
from apps.legacy_sim.models import LegacyJournalEntry
from apps.migration import activities
from apps.migration.models import StagedKind, StagedLegacyRow
from apps.migration.plan import build_config

PAGE_SIZE = 25
PAGES_PER_WINDOW = 2


def _job(dataset) -> MigrationJob:
    return MigrationJob.objects.create(
        customer_reference="Pagination test",
        source_system="legacy_erp",
        as_of_date=dataset.as_of_date,
    )


@pytest.mark.django_db(transaction=True)
def test_every_journal_page_is_staged_exactly_once_across_windows(seeded_legacy, in_process_legacy):
    job = _job(seeded_legacy)

    with override_settings(MIGRATION_PAGE_SIZE=PAGE_SIZE):
        source_count = LegacyJournalEntry.objects.count()
        expected_pages = (source_count + PAGE_SIZE - 1) // PAGE_SIZE
        assert expected_pages > 2, "this test only means something with several pages"

        page = 1
        windows = 0
        while True:
            result = activities.stage_entries(str(job.id), page, PAGES_PER_WINDOW)
            windows += 1
            assert windows < 100, "staging is not terminating"
            assert result["total_pages"] == expected_pages
            if result["done"]:
                break
            assert result["next_page"] > page, "the cursor must always advance"
            assert result["next_page"] <= result["last_page_staged"] + 1
            page = result["next_page"]

    staged = StagedLegacyRow.objects.filter(job=job, kind=StagedKind.ENTRY)
    assert windows > 1, "the window boundary was never exercised"
    assert staged.count() == source_count, "entries were skipped at a window boundary"

    # order_by() clears the model's default ordering, which Django otherwise adds to the
    # SELECT and which silently defeats .distinct() here.
    staged_pages = sorted(
        staged.order_by().values_list("source_page", flat=True).distinct()
    )
    assert staged_pages == list(range(1, expected_pages + 1)), "a page was skipped"


@pytest.mark.django_db(transaction=True)
def test_staging_a_window_past_the_end_terminates(seeded_legacy, in_process_legacy):
    job = _job(seeded_legacy)

    with override_settings(MIGRATION_PAGE_SIZE=PAGE_SIZE):
        source_count = LegacyJournalEntry.objects.count()
        beyond = (source_count // PAGE_SIZE) + 5
        result = activities.stage_entries(str(job.id), beyond, PAGES_PER_WINDOW)

    assert result["done"] is True
    assert result["rows_newly_staged"] == 0


def test_the_workflow_config_carries_the_chunk_sizes():
    config = build_config(batch_size=250, pages_per_stage_activity=3)
    assert config["batch_size"] == 250
    assert config["pages_per_stage_activity"] == 3
    assert config["max_chunks"] > 0
