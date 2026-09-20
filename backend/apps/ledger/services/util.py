"""Tiny helpers shared by the services."""

from __future__ import annotations

from decimal import Decimal

from django.utils import timezone

from ..models import AuditEvent


def q4(value) -> Decimal:
    """Quantise money to four places, the storage precision of every money column."""
    return Decimal(value).quantize(Decimal("0.0001"))


def audit(job, event_type: str, message: str, actor: str = "system", **payload) -> AuditEvent:
    """Append an audit event. Never update an existing one."""
    return AuditEvent.objects.create(
        job=job,
        event_type=event_type,
        actor=actor,
        message=message[:500],
        payload=payload or {},
    )


def now():
    return timezone.now()
