"""A deliberately awkward legacy ERP.

This app exists so that the integration work in the migration service is real rather
than described. It behaves the way a customer's ERP behaves: it paginates, it rate
limits you when you page too fast, it fails intermittently, and its trial balance
disagrees with its journal in ways that have different causes.

The last part is the point. ``InjectedAnomaly`` records exactly which account carries
which kind of disagreement, which is what lets the test suite assert that the
reconciliation classifier finds the right cause on the right account, instead of
asserting that it produced some number of rows.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import models


class AnomalyKind(models.TextChoices):
    # Balance reported rounded to two places, so the difference is under half a cent.
    ROUNDING = "ROUNDING", "Rounding"
    # Journal activity dated after the cutover, so an as-of comparison misses it.
    TIMING = "TIMING", "Timing"
    # Foreign currency account with a residual translation difference.
    FX = "FX", "Foreign exchange"
    # An unexplained difference with no benign explanation.
    TRUE_BREAK = "TRUE_BREAK", "True break"
    # Present in the trial balance, absent from the chart of accounts endpoint.
    MISSING_IN_LEDGER = "MISSING_IN_LEDGER", "Missing from chart of accounts"
    # Present in the chart of accounts, absent from the trial balance.
    MISSING_FROM_TRIAL_BALANCE = "MISSING_FROM_TRIAL_BALANCE", "Missing from trial balance"


class LegacyDataset(models.Model):
    """Singleton describing the generated dataset, so tests and the UI agree on it."""

    seeded_at = models.DateTimeField(auto_now=True)
    seed = models.IntegerField(default=0)
    window_start = models.DateField()
    window_end = models.DateField()
    as_of_date = models.DateField(help_text="Cutover date the reconciliation is expected to use.")
    account_count = models.IntegerField(default=0)
    entry_count = models.IntegerField(default=0)

    class Meta:
        verbose_name = "legacy dataset"

    def __str__(self) -> str:
        return f"legacy dataset seeded {self.seeded_at:%Y-%m-%d}"


class LegacyAccount(models.Model):
    code = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=160)
    type = models.CharField(max_length=16)
    normal_balance = models.CharField(max_length=8, default="DEBIT")
    currency = models.CharField(max_length=3, default="USD")
    is_active = models.BooleanField(default=True)
    # A retired account still shows in the chart of accounts but not in the trial balance.
    include_in_trial_balance = models.BooleanField(default=True)

    class Meta:
        ordering = ["code"]

    def __str__(self) -> str:
        return f"{self.code} {self.name}"


class LegacyJournalEntry(models.Model):
    ref = models.CharField(max_length=64, unique=True)
    entry_date = models.DateField()
    memo = models.CharField(max_length=400, blank=True, default="")
    status = models.CharField(max_length=8, default="POSTED")
    currency = models.CharField(max_length=3, default="USD")

    class Meta:
        ordering = ["entry_date", "ref"]
        indexes = [models.Index(fields=["entry_date"], name="legacy_entry_date_idx")]

    def __str__(self) -> str:
        return self.ref


class LegacyJournalLine(models.Model):
    entry = models.ForeignKey(LegacyJournalEntry, on_delete=models.CASCADE, related_name="lines")
    account = models.ForeignKey(LegacyAccount, on_delete=models.PROTECT, related_name="lines")
    ref = models.CharField(max_length=64)
    debit = models.DecimalField(max_digits=20, decimal_places=4, default=Decimal("0"))
    credit = models.DecimalField(max_digits=20, decimal_places=4, default=Decimal("0"))
    description = models.CharField(max_length=400, blank=True, default="")

    class Meta:
        ordering = ["entry_id", "id"]
        indexes = [models.Index(fields=["account", "entry"], name="legacy_line_acct_entry_idx")]

    def __str__(self) -> str:
        return f"{self.entry_id} {self.account_id}"


class InjectedAnomaly(models.Model):
    """One deliberate disagreement between the journal and the trial balance."""

    kind = models.CharField(max_length=32, choices=AnomalyKind.choices)
    account_code = models.CharField(max_length=32)
    detail = models.CharField(max_length=300, blank=True, default="")
    magnitude = models.DecimalField(max_digits=20, decimal_places=4, default=Decimal("0"))

    class Meta:
        ordering = ["kind", "account_code"]

    def __str__(self) -> str:
        return f"{self.kind} on {self.account_code}"
