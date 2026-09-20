"""HTTP surface of the simulated legacy ERP.

Deliberately unpleasant: bearer token required, paginated, rate limited, and it fails
intermittently. The migration client has to earn its data.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time

from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from . import ratelimit
from .models import LegacyAccount, LegacyJournalEntry
from .reporting import trial_balance

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000


def _guard(request: HttpRequest) -> JsonResponse | None:
    """Shared checks: auth, rate limit, injected failure."""
    expected = f"Bearer {settings.LEGACY_API_TOKEN}"
    if request.headers.get("Authorization") != expected:
        return JsonResponse({"error": "unauthorized"}, status=401)

    wait = ratelimit.rate_limit_exceeded()
    if wait is not None:
        response = JsonResponse(
            {"error": "rate limit exceeded", "retry_after": wait}, status=429
        )
        response["Retry-After"] = str(wait)
        return response

    if ratelimit.should_fail():
        return JsonResponse(
            {"error": "upstream unavailable", "hint": "the ERP does this under load"},
            status=503,
        )
    return None


def _paginate(request: HttpRequest, queryset, serialise) -> JsonResponse:
    try:
        page = max(1, int(request.GET.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(request.GET.get("page_size", DEFAULT_PAGE_SIZE))
    except (TypeError, ValueError):
        page_size = DEFAULT_PAGE_SIZE
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))

    total_items = queryset.count()
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    start = (page - 1) * page_size
    rows = list(queryset[start : start + page_size])

    return JsonResponse(
        {
            "data": [serialise(row) for row in rows],
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total_items": total_items,
                "total_pages": total_pages,
            },
        }
    )


def accounts(request: HttpRequest) -> JsonResponse:
    blocked = _guard(request)
    if blocked:
        return blocked

    def serialise(account: LegacyAccount) -> dict:
        return {
            "id": f"LACC-{account.code}",
            "code": account.code,
            "name": account.name,
            # The ERP spells some types with its own single letter codes.
            "type": {"ASSET": "A", "LIABILITY": "L", "EQUITY": "E", "REVENUE": "R", "EXPENSE": "X"}.get(
                account.type, account.type
            ),
            "currency": account.currency,
            "is_active": account.is_active,
        }

    return _paginate(request, LegacyAccount.objects.order_by("code"), serialise)


def journal_entries(request: HttpRequest) -> JsonResponse:
    blocked = _guard(request)
    if blocked:
        return blocked

    queryset = LegacyJournalEntry.objects.order_by("entry_date", "ref").prefetch_related(
        "lines__account"
    )
    since = request.GET.get("since")
    if since:
        queryset = queryset.filter(entry_date__gte=since)

    def serialise(entry: LegacyJournalEntry) -> dict:
        return {
            "id": entry.ref,
            "date": entry.entry_date.isoformat(),
            "memo": entry.memo,
            "status": entry.status,
            "currency": entry.currency,
            "lines": [
                {
                    "id": line.ref,
                    "account_code": line.account.code,
                    "debit": str(line.debit),
                    "credit": str(line.credit),
                    "description": line.description,
                }
                for line in entry.lines.all()
            ],
        }

    return _paginate(request, queryset, serialise)


def trial_balance_view(request: HttpRequest) -> JsonResponse:
    blocked = _guard(request)
    if blocked:
        return blocked
    return JsonResponse(trial_balance())


@csrf_exempt
def request_export(request: HttpRequest) -> JsonResponse:
    """Kick off an export and deliver it by webhook.

    Included because so much of the integration surface in this kind of role is
    asynchronous delivery with a signature to verify, rather than a request and reply.
    """
    blocked = _guard(request)
    if blocked:
        return blocked

    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid json"}, status=400)

    callback = body.get("callback_url")
    if not callback:
        return JsonResponse({"error": "callback_url is required"}, status=422)

    export_id = f"EXP-{int(time.time())}"
    payload = {
        "export_id": export_id,
        "status": "COMPLETED",
        "trial_balance": trial_balance(),
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    signature = hmac.new(
        settings.LEGACY_WEBHOOK_SECRET.encode(), encoded, hashlib.sha256
    ).hexdigest()

    def deliver() -> None:
        # A short delay so the API can answer first, the way a real export behaves.
        time.sleep(1.0)
        try:
            import requests

            requests.post(
                callback,
                data=encoded,
                headers={
                    "Content-Type": "application/json",
                    "X-Legacy-Signature": f"sha256={signature}",
                    "X-Legacy-Event": "export.completed",
                },
                timeout=10,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("export webhook delivery failed: %s", exc)

    threading.Thread(target=deliver, daemon=True).start()
    return JsonResponse({"export_id": export_id, "status": "ACCEPTED"}, status=202)
