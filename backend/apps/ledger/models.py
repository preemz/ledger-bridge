"""Domain models for the ledger.

Two database level guarantees matter here and are worth reading before the rest:

1. A journal entry must balance. That is enforced by a deferred constraint trigger
   (migration 0002), not only in Python, because an unbalanced entry that reaches
   the database through a shell session, an admin action or a future service is a
   corrupted ledger. DEFERRABLE INITIALLY DEFERRED means a load can insert the debit
   and the credit in any order and the check runs once at COMMIT.
2. Migration writes must be replayable. Every externally sourced row carries an
   ``external_ref`` from the source system and is upserted on
   ``(source_system, external_ref)``, so re-running a load that was interrupted is
   a no-op rather than a duplication.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from django.db import models


class AccountType(models.TextChoices):
    ASSET = "ASSET", "Asset"
    LIABILITY = "LIABILITY", "Liability"
    EQUITY = "EQUITY", "Equity"
    REVENUE = "REVENUE", "Revenue"
    EXPENSE = "EXPENSE", "Expense"


class NormalBalance(models.TextChoices):
    DEBIT = "DEBIT", "Debit"
    CREDIT = "CREDIT", "Credit"


# Account types whose balance increases on the debit side. Plain strings on purpose:
# a set of TextChoices members reads as an unhashable tuple to static checkers, and
# these values are compared against column values anyway.
DEBIT_NORMAL: frozenset[str] = frozenset({"ASSET", "EXPENSE"})


class EntryStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    POSTED = "POSTED", "Posted"
    VOID = "VOID", "Void"


class JobStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    RUNNING = "RUNNING", "Running"
    PAUSED = "PAUSED", "Paused"
    COMPLETED = "COMPLETED", "Completed"
    FAILED = "FAILED", "Failed"
    NEEDS_REVIEW = "NEEDS_REVIEW", "Needs review"

    @classmethod
    def terminal(cls) -> list[str]:
        return ["COMPLETED", "FAILED", "NEEDS_REVIEW"]


class PhaseName(models.TextChoices):
    EXTRACT = "EXTRACT", "Extract"
    STAGE = "STAGE", "Stage"
    TRANSFORM = "TRANSFORM", "Transform"
    VALIDATE = "VALIDATE", "Validate"
    LOAD = "LOAD", "Load"
    RECONCILE = "RECONCILE", "Reconcile"

    @classmethod
    def ordered(cls) -> list[str]:
        return ["EXTRACT", "STAGE", "TRANSFORM", "VALIDATE", "LOAD", "RECONCILE"]


class PhaseStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    RUNNING = "RUNNING", "Running"
    COMPLETED = "COMPLETED", "Completed"
    FAILED = "FAILED", "Failed"
    SKIPPED = "SKIPPED", "Skipped"


class BreakClassification(models.TextChoices):
    UNCLASSIFIED = "UNCLASSIFIED", "Unclassified"
    ROUNDING = "ROUNDING", "Rounding"
    TIMING = "TIMING", "Timing"
    FX = "FX", "Foreign exchange"
    MAPPING_ERROR = "MAPPING_ERROR", "Mapping error"
    TRUE_BREAK = "TRUE_BREAK", "True break"


class Severity(models.TextChoices):
    LOW = "LOW", "Low"
    MEDIUM = "MEDIUM", "Medium"
    HIGH = "HIGH", "High"


MONEY = {"max_digits": 20, "decimal_places": 4, "default": Decimal("0")}


class TimeStamped(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Account(TimeStamped):
    """A row in the customer's chart of accounts."""

    code = models.CharField(max_length=32, db_index=True)
    name = models.CharField(max_length=160)
    type = models.CharField(max_length=16, choices=AccountType.choices)
    normal_balance = models.CharField(max_length=8, choices=NormalBalance.choices, db_index=True)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="children"
    )
    currency = models.CharField(max_length=3, default="USD")
    is_active = models.BooleanField(default=True)
    # Identifier in the source system. The upsert key for the load phase.
    external_ref = models.CharField(max_length=64, blank=True, default="")
    source_system = models.CharField(max_length=32, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["external_ref"],
                condition=~models.Q(external_ref=""),
                name="uniq_account_external_ref",
            ),
            models.UniqueConstraint(fields=["code"], name="uniq_account_code"),
        ]
        ordering = ["code"]

    def __str__(self) -> str:
        return f"{self.code} {self.name}"

    def save(self, *args, **kwargs):
        # The normal balance is a function of the account type. Deriving it here keeps
        # callers from having to remember the rule, and the reconciliation report
        # depends on it being right.
        self.normal_balance = (
            NormalBalance.DEBIT if self.type in DEBIT_NORMAL else NormalBalance.CREDIT
        )
        return super().save(*args, **kwargs)

    def signed_balance(self, net_debit: Decimal) -> Decimal:
        """Net debit expressed in the account's own normal direction."""
        return net_debit if self.normal_balance == NormalBalance.DEBIT else -net_debit


class JournalEntry(TimeStamped):
    source_system = models.CharField(max_length=32, db_index=True)
    external_ref = models.CharField(max_length=64)
    entry_date = models.DateField(db_index=True)
    memo = models.CharField(max_length=400, blank=True, default="")
    status = models.CharField(max_length=8, choices=EntryStatus.choices, default=EntryStatus.POSTED)
    currency = models.CharField(max_length=3, default="USD")
    fx_rate = models.DecimalField(max_digits=18, decimal_places=8, default=Decimal("1"))
    source_created_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            # The idempotency key for a whole entry.
            models.UniqueConstraint(
                fields=["source_system", "external_ref"], name="uniq_entry_source_ref"
            ),
        ]
        indexes = [
            models.Index(fields=["entry_date", "status"], name="entry_date_status_idx"),
        ]
        ordering = ["entry_date", "id"]

    def __str__(self) -> str:
        return f"{self.source_system}:{self.external_ref}"

    @property
    def totals(self) -> tuple[Decimal, Decimal]:
        agg = self.lines.aggregate(
            debit=models.Sum("debit"), credit=models.Sum("credit")
        )
        return agg["debit"] or Decimal("0"), agg["credit"] or Decimal("0")


class JournalLine(TimeStamped):
    entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name="lines")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="lines")
    debit = models.DecimalField(**MONEY)
    credit = models.DecimalField(**MONEY)
    description = models.CharField(max_length=400, blank=True, default="")
    external_ref = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(debit__gt=0, credit__gt=0), name="line_single_sided"
            ),
            models.CheckConstraint(
                condition=models.Q(debit__gt=0) | models.Q(credit__gt=0), name="line_not_empty"
            ),
            models.CheckConstraint(
                condition=models.Q(debit__gte=0) & models.Q(credit__gte=0),
                name="line_non_negative",
            ),
            models.UniqueConstraint(
                fields=["entry", "external_ref"],
                condition=~models.Q(external_ref=""),
                name="uniq_line_entry_external_ref",
            ),
        ]
        indexes = [
            models.Index(fields=["account", "entry"], name="line_account_entry_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.entry_id} {self.account_id} D{self.debit} C{self.credit}"


class MigrationJob(TimeStamped):
    """One customer project: move one source system's books into the ledger."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer_reference = models.CharField(max_length=120)
    source_system = models.CharField(max_length=32)
    as_of_date = models.DateField()
    status = models.CharField(max_length=16, choices=JobStatus.choices, default=JobStatus.PENDING)
    temporal_workflow_id = models.CharField(max_length=200, blank=True, default="")
    temporal_run_id = models.CharField(max_length=200, blank=True, default="")
    executor = models.CharField(max_length=16, blank=True, default="")
    counters = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True, default="")
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.customer_reference} ({self.status})"

    def bump(self, **deltas: int) -> None:
        """Increment counters atomically enough for an operator display.

        A migration is a single-writer process per job, so a read, update and save is
        sufficient. The counter is a progress indicator, never a source of truth: the
        reconciliation report and the row counts are.
        """
        counters = dict(self.counters or {})
        for key, value in deltas.items():
            counters[key] = int(counters.get(key, 0)) + int(value)
        self.counters = counters
        self.save(update_fields=["counters", "updated_at"])

    def set_counters(self, **values: int) -> None:
        counters = dict(self.counters or {})
        counters.update({k: int(v) for k, v in values.items()})
        self.counters = counters
        self.save(update_fields=["counters", "updated_at"])


class MigrationPhase(TimeStamped):
    """A durable checkpoint.

    ``idempotency_key`` is generated from the job and the phase name, so a retried
    activity resolves to the same row and can tell whether the work already ran.
    """

    job = models.ForeignKey(MigrationJob, on_delete=models.CASCADE, related_name="phases")
    name = models.CharField(max_length=16, choices=PhaseName.choices)
    status = models.CharField(max_length=12, choices=PhaseStatus.choices, default=PhaseStatus.PENDING)
    attempt = models.PositiveIntegerField(default=1)
    # Long phases are split into independently retryable chunks. Each chunk is its own
    # durable checkpoint, so a crash costs one chunk of work rather than the whole phase.
    chunk = models.PositiveIntegerField(default=0)
    # Discriminates two kinds of work that share a phase name (the chart of accounts and
    # the journal both load under LOAD), so a checkpoint is never mistaken for another.
    label = models.CharField(max_length=64, blank=True, default="")
    idempotency_key = models.CharField(max_length=200, unique=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    stats = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["job", "id"]

    def __str__(self) -> str:
        return f"{self.job_id} {self.name} #{self.attempt}.{self.chunk} {self.status}"

    @property
    def duration_ms(self) -> int | None:
        if not self.started_at or not self.finished_at:
            return None
        return int((self.finished_at - self.started_at).total_seconds() * 1000)


class AuditEvent(models.Model):
    """Append only. The database refuses UPDATE and DELETE (migration 0002).

    A migration that a finance team signs off on has to be explainable months later,
    which means the log cannot be editable by the application it describes.

    ``PROTECT`` rather than ``CASCADE`` on the job: the trigger already refuses the row
    deletes a cascade would issue, so a cascade produced a confusing database error
    naming the trigger instead of a clear "cannot delete a job that has an audit trail".
    The job is the subject of the audit, and the point of an audit trail is that neither
    it nor its subject can quietly disappear.
    """

    job = models.ForeignKey(
        MigrationJob, null=True, blank=True, on_delete=models.PROTECT, related_name="audit"
    )
    event_type = models.CharField(max_length=48, db_index=True)
    actor = models.CharField(max_length=64, default="system")
    message = models.CharField(max_length=500)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.event_type}: {self.message}"


class ReconciliationRun(TimeStamped):
    """One comparison of the loaded ledger against the customer's legacy trial balance."""

    job = models.ForeignKey(MigrationJob, on_delete=models.CASCADE, related_name="reconciliation_runs")
    as_of_date = models.DateField()
    status = models.CharField(max_length=16, default="COMPLETED")
    legacy_total_debit = models.DecimalField(**MONEY)
    legacy_total_credit = models.DecimalField(**MONEY)
    loaded_total_debit = models.DecimalField(**MONEY)
    loaded_total_credit = models.DecimalField(**MONEY)
    legacy_entry_count = models.IntegerField(default=0)
    loaded_entry_count = models.IntegerField(default=0)
    difference = models.DecimalField(**MONEY)
    duration_ms = models.IntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"reconciliation for {self.job_id} as of {self.as_of_date}"


class ReconciliationBreak(models.Model):
    """A single account whose loaded balance disagrees with the legacy balance."""

    run = models.ForeignKey(ReconciliationRun, on_delete=models.CASCADE, related_name="breaks")
    account_code = models.CharField(max_length=32)
    account_name = models.CharField(max_length=160, blank=True, default="")
    legacy_balance = models.DecimalField(**MONEY)
    loaded_balance = models.DecimalField(**MONEY)
    variance = models.DecimalField(**MONEY)
    # Position of this break when the accounts are ranked by absolute variance, and the
    # share of the total unexplained amount accounted for by this break and all larger
    # ones. Together they answer "how far down the list do I have to read", which is the
    # question a finance lead actually asks. Both are computed in the variance query.
    variance_rank = models.IntegerField(default=0)
    cumulative_variance_pct = models.DecimalField(
        max_digits=7, decimal_places=2, default=Decimal("0")
    )
    classification = models.CharField(
        max_length=16, choices=BreakClassification.choices, default=BreakClassification.UNCLASSIFIED
    )
    severity = models.CharField(max_length=8, choices=Severity.choices, default=Severity.LOW)
    resolved = models.BooleanField(default=False)
    resolved_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=400, blank=True, default="")

    class Meta:
        ordering = ["variance_rank", "account_code"]
        indexes = [
            models.Index(fields=["run", "resolved"], name="break_run_resolved_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.account_code} variance {self.variance}"
