"""Serializers.

Money goes over the wire as a decimal string, which is the DRF default and the right
choice: a float in JSON loses precision, and a reconciliation console that shows
pennies has to be exact.
"""

from __future__ import annotations

from django.utils import timezone
from rest_framework import serializers

from apps.legacy_sim.models import LegacyDataset

from .models import (
    AuditEvent,
    BreakClassification,
    MigrationJob,
    MigrationPhase,
    ReconciliationBreak,
    ReconciliationRun,
)

SOURCE_SYSTEMS = ("legacy_erp", "netsuite", "xero")
TERMINAL_STATUSES = {"COMPLETED", "FAILED", "NEEDS_REVIEW"}


class PhaseSerializer(serializers.ModelSerializer):
    duration_ms = serializers.IntegerField(read_only=True)

    class Meta:
        model = MigrationPhase
        fields = (
            "id",
            "name",
            "status",
            "attempt",
            "chunk",
            "label",
            "started_at",
            "finished_at",
            "duration_ms",
            "stats",
            "error",
        )


class BreakSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReconciliationBreak
        fields = (
            "id",
            "run",
            "account_code",
            "account_name",
            "legacy_balance",
            "loaded_balance",
            "variance",
            "variance_rank",
            "cumulative_variance_pct",
            "classification",
            "severity",
            "resolved",
            "resolved_at",
            "note",
        )
        read_only_fields = ("id", "run", "resolved_at")


class RunSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReconciliationRun
        fields = (
            "id",
            "job",
            "as_of_date",
            "status",
            "legacy_total_debit",
            "legacy_total_credit",
            "loaded_total_debit",
            "loaded_total_credit",
            "legacy_entry_count",
            "loaded_entry_count",
            "difference",
            "duration_ms",
            "created_at",
        )


class AuditEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditEvent
        fields = ("id", "event_type", "actor", "message", "payload", "created_at")


class JobSerializer(serializers.ModelSerializer):
    class Meta:
        model = MigrationJob
        fields = (
            "id",
            "customer_reference",
            "source_system",
            "as_of_date",
            "status",
            "executor",
            "temporal_workflow_id",
            "counters",
            "error",
            "started_at",
            "finished_at",
            "created_at",
        )


class JobDetailSerializer(JobSerializer):
    phases = serializers.SerializerMethodField()
    reconciliation = serializers.SerializerMethodField()

    class Meta(JobSerializer.Meta):
        fields = JobSerializer.Meta.fields + ("phases", "reconciliation")

    def get_phases(self, job: MigrationJob):
        return PhaseSerializer(job.phases.order_by("id"), many=True).data

    def get_reconciliation(self, job: MigrationJob):
        run = job.reconciliation_runs.order_by("-created_at").first()
        if run is None:
            return None
        return {
            "run": RunSerializer(run).data,
            "breaks": BreakSerializer(run.breaks.all(), many=True).data,
        }


class CreateJobSerializer(serializers.Serializer):
    customer_reference = serializers.CharField(max_length=120)
    source_system = serializers.ChoiceField(choices=SOURCE_SYSTEMS, default="legacy_erp")
    as_of_date = serializers.DateField(required=False)

    def validate_as_of_date(self, value):
        if value > timezone.now().date():
            raise serializers.ValidationError("as_of_date cannot be in the future")
        return value

    def resolved_as_of_date(self):
        """Default to the cutover date of the seeded dataset when one exists.

        The seeded books contain activity after that date on purpose, so using it is
        what produces a reconciliation report with interesting breaks in it. A date of
        today would include that activity and quietly hide them.
        """
        supplied = self.validated_data.get("as_of_date")
        if supplied:
            return supplied
        dataset = LegacyDataset.objects.order_by("-seeded_at").first()
        return dataset.as_of_date if dataset else timezone.now().date()


class ResolveBreakSerializer(serializers.Serializer):
    classification = serializers.ChoiceField(choices=BreakClassification.choices, required=False)
    note = serializers.CharField(max_length=400, required=False, allow_blank=True)
    resolved = serializers.BooleanField(required=False, default=True)