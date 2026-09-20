"""Staging table.

Extract and transform are intentionally separate from load. The legacy system is a
flaky, rate limited third party: when it is being difficult you want the raw pages
landing somewhere durable so the expensive read is not repeated every time a later
phase fails. Staging is also the point where a resume can tell what it already has,
which is what makes the extract phase re-runnable without hammering the customer's
ERP a second time.
"""

from __future__ import annotations

from django.db import models


class StagedKind(models.TextChoices):
    ACCOUNT = "ACCOUNT", "Account"
    ENTRY = "ENTRY", "Journal entry"


class StagedLegacyRow(models.Model):
    job = models.ForeignKey(
        "ledger.MigrationJob", on_delete=models.CASCADE, related_name="staged_rows"
    )
    kind = models.CharField(max_length=8, choices=StagedKind.choices)
    external_ref = models.CharField(max_length=64)
    source_page = models.PositiveIntegerField(default=1)
    payload = models.JSONField()
    # Populated by the TRANSFORM phase. Kept on the same row so a resumed transform
    # can skip rows it has already normalised instead of redoing the whole table.
    normalised = models.JSONField(null=True, blank=True)
    staged_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["job", "kind", "external_ref"], name="uniq_staged_job_kind_ref"
            ),
        ]
        indexes = [
            models.Index(fields=["job", "kind"], name="staged_job_kind_idx"),
        ]
        ordering = ["job", "kind", "source_page", "id"]

    def __str__(self) -> str:
        return f"{self.kind}:{self.external_ref}"
