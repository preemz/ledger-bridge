"""Audit hooks.

Only transitions are logged, not every save. A migration bumps progress counters
constantly and an audit log that fills with counter updates is an audit log nobody
reads. The events that matter are a status change on the job and a phase finishing
or failing.
"""

from __future__ import annotations

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .models import MigrationJob, MigrationPhase, PhaseStatus


@receiver(pre_save, sender=MigrationJob)
def remember_job_status(sender, instance: MigrationJob, **kwargs) -> None:
    instance._previous_status = None
    if instance.pk:
        instance._previous_status = (
            MigrationJob.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
        )


@receiver(post_save, sender=MigrationJob)
def log_job_status(sender, instance: MigrationJob, created: bool, **kwargs) -> None:
    previous = getattr(instance, "_previous_status", None)
    if created:
        from .services.util import audit

        audit(instance, "job.created", f"migration requested for {instance.customer_reference}")
        return
    if previous is None or previous == instance.status:
        return
    from .services.util import audit

    audit(
        instance,
        "job.status_changed",
        f"status {previous} -> {instance.status}",
        actor=instance.executor or "system",
        **({"error": instance.error[:400]} if instance.error else {}),
    )


@receiver(post_save, sender=MigrationPhase)
def log_phase_failure(sender, instance: MigrationPhase, created: bool, **kwargs) -> None:
    """Audit phase failures and skips, not phase successes.

    A phase is executed in chunks, so a successful 4,000 entry load writes a dozen
    COMPLETED rows and the audit log fills with work that the phase timeline already
    shows, complete with per chunk stats. The events worth being able to find months
    later are the ones that went wrong, so those are the ones recorded here.
    """
    if instance.status not in {PhaseStatus.FAILED, PhaseStatus.SKIPPED}:
        return
    from .services.util import audit

    audit(
        instance.job,
        f"phase.{instance.status.lower()}",
        f"{instance.name} attempt {instance.attempt} chunk {instance.chunk} {instance.status}",
        **({"stats": instance.stats} if instance.stats else {}),
        **({"error": instance.error[:400]} if instance.error else {}),
    )
