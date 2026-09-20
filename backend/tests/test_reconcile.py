"""Reconciliation must name the cause, not just count the rows.

The dataset generator records every disagreement it injected. These tests assert that
the classifier lands on the right classification for the right account, which is the
only version of this that means anything: a test that asserts "five breaks were found"
passes just as happily when all five are attributed to the wrong reason.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apps.ledger.models import MigrationJob, ReconciliationBreak, ReconciliationRun
from apps.legacy_sim.models import AnomalyKind, InjectedAnomaly
from apps.migration.runner import run_inline

# Each injected anomaly, and the classification the operator should be shown for it.
EXPECTED = {
    AnomalyKind.ROUNDING: "ROUNDING",
    AnomalyKind.FX: "FX",
    AnomalyKind.TIMING: "TIMING",
    AnomalyKind.TRUE_BREAK: "TRUE_BREAK",
    AnomalyKind.MISSING_IN_LEDGER: "MAPPING_ERROR",
    AnomalyKind.MISSING_FROM_TRIAL_BALANCE: "MAPPING_ERROR",
}


@pytest.fixture
def compiled_run(seeded_legacy, in_process_legacy):
    job = MigrationJob.objects.create(
        customer_reference="Reconciliation test",
        source_system="legacy_erp",
        as_of_date=seeded_legacy.as_of_date,
    )
    result = run_inline(str(job.id))
    # The runner writes through its own instance, so the fixture's object is stale.
    job.refresh_from_db()
    run = ReconciliationRun.objects.filter(job=job).order_by("-created_at").first()
    return job, run, result


@pytest.mark.django_db(transaction=True)
def test_every_injected_anomaly_is_found_and_correctly_classified(compiled_run):
    job, run, result = compiled_run
    assert run is not None, result

    breaks = {brk.account_code: brk for brk in ReconciliationBreak.objects.filter(run=run)}
    anomalies = list(InjectedAnomaly.objects.all())
    assert anomalies, "the dataset generator injected nothing, so this test proves nothing"

    for anomaly in anomalies:
        brk = breaks.get(anomaly.account_code)
        assert brk is not None, (
            f"{anomaly.kind} was injected on account {anomaly.account_code} "
            "but no break was reported for it"
        )
        expected = EXPECTED[anomaly.kind]
        assert brk.classification == expected, (
            f"account {anomaly.account_code} carries an injected {anomaly.kind} "
            f"but was classified {brk.classification} (variance {brk.variance})"
        )
        assert brk.note, "a break with no explanation is not actionable"


@pytest.mark.django_db(transaction=True)
def test_high_severity_breaks_hold_the_job_for_review(compiled_run):
    job, run, result = compiled_run
    assert ReconciliationBreak.objects.filter(run=run, resolved=False, severity="HIGH").exists()
    assert job.status == "NEEDS_REVIEW"


@pytest.mark.django_db(transaction=True)
def test_the_reconciliation_records_both_sides_of_the_comparison(compiled_run):
    job, run, _ = compiled_run
    assert run.legacy_entry_count > 0
    assert run.loaded_entry_count > 0
    assert run.legacy_total_debit > 0
    assert run.loaded_total_debit > 0
    # The migrated ledger and the legacy trial balance disagree, which is the entire
    # reason the reconciliation exists. A zero difference here would mean the seeded
    # anomalies were not loaded, not that the migration was flawless.
    assert run.difference != 0


@pytest.mark.django_db(transaction=True)
def test_both_sides_are_measured_the_same_way(compiled_run):
    """The headline difference has to be a difference between like quantities.

    An earlier version summed all debit movement on the ledger side while the customer's
    side summed positive net balances. Both numbers were correct and the difference
    between them was meaningless, which is the worst kind of reconciliation output: it
    looks like a finding. This asserts the two sides stay within a few percent, which
    the movement convention failed by a factor of more than two.
    """
    job, run, _ = compiled_run
    legacy = Decimal(run.legacy_total_debit)
    loaded = Decimal(run.loaded_total_debit)
    relative_gap = abs(loaded - legacy) / legacy
    assert relative_gap < Decimal("0.02"), (
        f"the two sides are not comparable: legacy {legacy} vs loaded {loaded}"
    )

    # A trial balance balances on both sides, up to the differences deliberately injected
    # into the dataset. A large debit-versus-credit gap means one side is mis-defined.
    assert abs(Decimal(run.loaded_total_credit) - loaded) < loaded * Decimal("0.02")
    assert abs(Decimal(run.legacy_total_credit) - legacy) < legacy * Decimal("0.02")


@pytest.mark.django_db(transaction=True)
def test_rerunning_the_reconciliation_replaces_rather_than_appends(compiled_run):
    """A Temporal retry of the reconcile activity must be indistinguishable from the first run."""
    job, run, _ = compiled_run
    from apps.migration import activities

    breaks_before = ReconciliationBreak.objects.count()
    activities.reconcile(str(job.id))

    runs = ReconciliationRun.objects.filter(job=job)
    assert runs.count() == 1, "a retry of the reconcile activity added a second run"
    latest = runs.first()
    assert ReconciliationBreak.objects.filter(run=latest).count() == breaks_before
    assert ReconciliationBreak.objects.count() == breaks_before


@pytest.mark.django_db(transaction=True)
def test_breaks_are_ranked_by_size_with_a_running_share(compiled_run):
    job, run, _ = compiled_run
    breaks = list(run.breaks.all())
    assert breaks, "no breaks to rank"

    assert [brk.variance_rank for brk in breaks] == list(range(1, len(breaks) + 1))
    magnitudes = [abs(brk.variance) for brk in breaks]
    assert magnitudes == sorted(magnitudes, reverse=True)

    # The running share is what answers "how far down the list do I have to read".
    assert breaks[0].cumulative_variance_pct > 0
    assert breaks[-1].cumulative_variance_pct == Decimal("100.00")
    assert all(
        breaks[i].cumulative_variance_pct <= breaks[i + 1].cumulative_variance_pct
        for i in range(len(breaks) - 1)
    )
