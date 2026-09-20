"""The operator API.

Thin on purpose. Everything interesting happens in services and in the workflow; these
views validate input, call one thing, and serialise the result.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.legacy_sim.models import LegacyDataset
from apps.migration.starter import send_signal, start_migration

from .models import (
    Account,
    AuditEvent,
    JobStatus,
    JournalEntry,
    JournalLine,
    MigrationJob,
    ReconciliationBreak,
)
from .serializers import (
    AuditEventSerializer,
    BreakSerializer,
    CreateJobSerializer,
    JobDetailSerializer,
    JobSerializer,
    ResolveBreakSerializer,
)
from .services.util import audit

logger = logging.getLogger(__name__)


class JobListCreateView(APIView):
    def get(self, request: Request) -> Response:
        jobs = MigrationJob.objects.all()[:200]
        return Response(JobSerializer(jobs, many=True).data)

    def post(self, request: Request) -> Response:
        serializer = CreateJobSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        job = MigrationJob.objects.create(
            customer_reference=data["customer_reference"],
            source_system=data["source_system"],
            as_of_date=serializer.resolved_as_of_date(),
            status=JobStatus.PENDING,
        )
        start_migration(job)
        job.refresh_from_db()
        return Response(JobDetailSerializer(job).data, status=status.HTTP_201_CREATED)


class JobDetailView(APIView):
    def get(self, request: Request, job_id) -> Response:
        job = MigrationJob.objects.filter(pk=job_id).first()
        if job is None:
            return Response({"detail": "not found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(JobDetailSerializer(job).data)


class JobAuditView(APIView):
    def get(self, request: Request, job_id) -> Response:
        events = AuditEvent.objects.filter(job_id=job_id)[:500]
        return Response(AuditEventSerializer(events, many=True).data)


class JobReconciliationView(APIView):
    def get(self, request: Request, job_id) -> Response:
        job = MigrationJob.objects.filter(pk=job_id).first()
        if job is None:
            return Response({"detail": "not found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(JobDetailSerializer(job).data.get("reconciliation"))


class JobSignalView(APIView):
    """Pause, resume or cancel.

    Returns 200 with ok=false and a reason when the signal cannot be delivered, rather
    than an error status. An operator pressing pause on an inline run should get an
    explanation, not a red toast.
    """

    def post(self, request: Request, job_id) -> Response:
        job = MigrationJob.objects.filter(pk=job_id).first()
        if job is None:
            return Response({"detail": "not found"}, status=status.HTTP_404_NOT_FOUND)

        signal = str(request.data.get("signal") or "").strip().lower()
        if signal not in {"pause", "resume", "cancel"}:
            return Response(
                {"detail": "signal must be one of pause, resume, cancel"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = send_signal(job, signal)
        job.refresh_from_db()
        payload = {
            "ok": bool(result.get("ok")),
            "signal": signal,
            "status": job.status,
            "detail": result.get("detail", ""),
        }
        return Response(payload, status=status.HTTP_200_OK)


class BreakResolveView(APIView):
    def post(self, request: Request, break_id) -> Response:
        brk = ReconciliationBreak.objects.filter(pk=break_id).first()
        if brk is None:
            return Response({"detail": "not found"}, status=status.HTTP_404_NOT_FOUND)

        serializer = ResolveBreakSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if "classification" in data:
            brk.classification = data["classification"]
        if "note" in data:
            brk.note = data["note"][:400]
        brk.resolved = data.get("resolved", True)
        brk.resolved_at = timezone.now() if brk.resolved else None
        brk.save(update_fields=["classification", "note", "resolved", "resolved_at"])

        audit(
            brk.run.job,
            "break.resolved" if brk.resolved else "break.reopened",
            f"{brk.account_code} marked {brk.classification}",
            account_code=brk.account_code,
            variance=str(brk.variance),
        )
        return Response(BreakSerializer(brk).data)


class StatsView(APIView):
    def get(self, request: Request) -> Response:
        dataset = LegacyDataset.objects.order_by("-seeded_at").first()
        return Response(
            {
                "jobs_total": MigrationJob.objects.count(),
                "jobs_running": MigrationJob.objects.filter(
                    status__in=[JobStatus.RUNNING, JobStatus.PENDING, JobStatus.PAUSED]
                ).count(),
                "jobs_completed": MigrationJob.objects.filter(
                    status__in=[JobStatus.COMPLETED, JobStatus.NEEDS_REVIEW]
                ).count(),
                "breaks_open": ReconciliationBreak.objects.filter(resolved=False).count(),
                # Counted from the ledger tables, not summed from each job's counters.
                # Every job's counter is a ledger wide count, so summing them multiplied
                # the figure by the number of jobs: three jobs reported 12,471 entries
                # against 4,157 actually in the table. A wrong number on the first screen
                # a reviewer looks at is worth a query.
                "entries_loaded": JournalEntry.objects.count(),
                "lines_loaded": JournalLine.objects.count(),
                "accounts_loaded": Account.objects.count(),
                "dataset_as_of_date": dataset.as_of_date if dataset else None,
            }
        )


@csrf_exempt
def legacy_webhook(request):
    """Inbound webhook from the legacy ERP: verify the signature before trusting it.

    A webhook endpoint that parses the body before checking the signature is a webhook
    endpoint that will happily accept a forged export from anyone who knows the URL.
    The comparison is constant time.
    """
    if request.method != "POST":
        return JsonResponse({"detail": "method not allowed"}, status=405)

    body = request.body or b""
    signature = request.headers.get("X-Legacy-Signature", "")
    expected = hmac.new(
        request_signing_secret(), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, f"sha256={expected}"):
        logger.warning("rejected legacy webhook with a bad signature")
        return JsonResponse({"detail": "invalid signature"}, status=401)

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return JsonResponse({"detail": "invalid json"}, status=400)

    job = MigrationJob.objects.order_by("-created_at").first()
    if job is not None:
        audit(
            job,
            "webhook.export_completed",
            f"legacy export {payload.get('export_id')} delivered",
            export_id=payload.get("export_id"),
            event=request.headers.get("X-Legacy-Event", ""),
        )
    return JsonResponse({"ok": True, "export_id": payload.get("export_id")})


def request_signing_secret() -> bytes:
    from django.conf import settings

    return settings.LEGACY_WEBHOOK_SECRET.encode()