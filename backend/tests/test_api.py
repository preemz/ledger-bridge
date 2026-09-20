"""The API contract the operator console is written against.

These tests exist so that the frontend and the backend cannot drift: if a field is
renamed here, the console breaks in a test rather than in front of a customer.
"""

from __future__ import annotations

import json

import pytest
from django.test import Client

from apps.ledger.models import MigrationJob

ENDPOINTS = Client()


@pytest.fixture
def no_autostart(monkeypatch):
    """Stop POST /api/jobs/ from launching a migration in the background.

    Without this the API spawns an inline runner thread which races the test, and racing
    a thread that writes to tables pytest is about to truncate produces failures that
    look like application bugs and are not. The job is driven by run_inline instead, so
    the assertions see a finished migration.
    """
    started: list[str] = []

    def fake_start(job):
        started.append(str(job.id))
        return {"executor": "test"}

    monkeypatch.setattr("apps.ledger.views.start_migration", fake_start)
    return started


@pytest.mark.django_db
def test_health():
    response = ENDPOINTS.get("/api/health/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.django_db
def test_stats_has_the_shape_the_console_reads():
    response = ENDPOINTS.get("/api/stats/")
    assert response.status_code == 200
    payload = response.json()
    for key in ("jobs_total", "jobs_running", "jobs_completed", "breaks_open", "entries_loaded"):
        assert key in payload
        assert isinstance(payload[key], int), f"{key} must be a number, the console renders it directly"


@pytest.mark.django_db(transaction=True)
def test_create_job_then_read_it_back(seeded_legacy, in_process_legacy, no_autostart):
    response = ENDPOINTS.post(
        "/api/jobs/",
        data=json.dumps(
            {
                "customer_reference": "Northwind Trading",
                "source_system": "legacy_erp",
            }
        ),
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    job = response.json()
    assert job["customer_reference"] == "Northwind Trading"
    assert job["status"] == "PENDING"
    assert job["phases"] == []
    assert job["reconciliation"] is None
    # as_of_date defaulted to the seeded cutover date, which is what makes the demo
    # produce interesting breaks.
    assert job["as_of_date"] == str(seeded_legacy.as_of_date)

    job_id = job["id"]
    assert no_autostart == [job_id], "the API should have started the migration exactly once"

    from apps.migration.runner import run_inline

    run_inline(job_id)

    detail = ENDPOINTS.get(f"/api/jobs/{job_id}/").json()
    assert detail["status"] in {"COMPLETED", "NEEDS_REVIEW"}
    phase_names = {phase["name"] for phase in detail["phases"]}
    assert {"EXTRACT", "STAGE", "TRANSFORM", "VALIDATE", "LOAD", "RECONCILE"} <= phase_names
    for phase in detail["phases"]:
        assert phase["status"] in {"PENDING", "RUNNING", "COMPLETED", "FAILED", "SKIPPED"}

    reconciliation = detail["reconciliation"]
    assert reconciliation is not None
    assert reconciliation["run"]["as_of_date"] == str(seeded_legacy.as_of_date)
    assert reconciliation["breaks"], "the seeded dataset should produce breaks"

    brk = reconciliation["breaks"][0]
    for key in (
        "id",
        "account_code",
        "legacy_balance",
        "loaded_balance",
        "variance",
        "classification",
        "severity",
        "resolved",
        "note",
    ):
        assert key in brk

    audit = ENDPOINTS.get(f"/api/jobs/{job_id}/audit/").json()
    assert audit and all("event_type" in event and "created_at" in event for event in audit)

    stats = ENDPOINTS.get("/api/stats/").json()
    assert stats["jobs_total"] == 1
    assert stats["entries_loaded"] > 0
    assert stats["breaks_open"] == len(reconciliation["breaks"])


@pytest.mark.django_db
def test_create_job_validates_input():
    response = ENDPOINTS.post(
        "/api/jobs/",
        data=json.dumps({"customer_reference": "x", "source_system": "sap"}),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert "source_system" in response.json()


@pytest.mark.django_db
def test_unknown_job_is_a_404():
    assert ENDPOINTS.get("/api/jobs/00000000-0000-0000-0000-000000000000/").status_code == 404


@pytest.mark.django_db(transaction=True)
def test_resolving_a_break_is_recorded(seeded_legacy, in_process_legacy, no_autostart):
    from apps.migration.runner import run_inline

    job = MigrationJob.objects.create(
        customer_reference="Resolve test",
        source_system="legacy_erp",
        as_of_date=seeded_legacy.as_of_date,
    )
    run_inline(str(job.id))

    detail = ENDPOINTS.get(f"/api/jobs/{job.id}/").json()
    brk = detail["reconciliation"]["breaks"][0]

    response = ENDPOINTS.post(
        f"/api/breaks/{brk['id']}/resolve/",
        data=json.dumps({"classification": "TIMING", "note": "Confirmed with the finance lead"}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    payload = response.json()
    assert payload["classification"] == "TIMING"
    assert payload["resolved"] is True
    assert payload["resolved_at"] is not None

    audit = ENDPOINTS.get(f"/api/jobs/{job.id}/audit/").json()
    assert any(event["event_type"] == "break.resolved" for event in audit)


@pytest.mark.django_db
def test_signal_is_rejected_with_an_explanation_when_not_on_temporal():
    job = MigrationJob.objects.create(
        customer_reference="Signal test", source_system="legacy_erp", as_of_date="2026-01-01"
    )
    response = ENDPOINTS.post(
        f"/api/jobs/{job.id}/signal/",
        data=json.dumps({"signal": "pause"}),
        content_type="application/json",
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert "inline runner" in payload["detail"]


@pytest.mark.django_db
def test_signal_rejects_an_unknown_value():
    job = MigrationJob.objects.create(
        customer_reference="Signal test", source_system="legacy_erp", as_of_date="2026-01-01"
    )
    response = ENDPOINTS.post(
        f"/api/jobs/{job.id}/signal/",
        data=json.dumps({"signal": "explode"}),
        content_type="application/json",
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_webhook_rejects_a_bad_signature():
    response = ENDPOINTS.post(
        "/api/webhooks/legacy/",
        data=json.dumps({"export_id": "EXP-1"}),
        content_type="application/json",
        HTTP_X_LEGACY_SIGNATURE="sha256=deadbeef",
    )
    assert response.status_code == 401


@pytest.mark.django_db
def test_webhook_accepts_a_correctly_signed_delivery():
    import hashlib
    import hmac

    from django.conf import settings

    body = json.dumps({"export_id": "EXP-1", "status": "COMPLETED"}, separators=(",", ":"), sort_keys=True)
    signature = hmac.new(
        settings.LEGACY_WEBHOOK_SECRET.encode(), body.encode(), hashlib.sha256
    ).hexdigest()

    response = ENDPOINTS.post(
        "/api/webhooks/legacy/",
        data=body,
        content_type="application/json",
        HTTP_X_LEGACY_SIGNATURE=f"sha256={signature}",
        HTTP_X_LEGACY_EVENT="export.completed",
    )
    assert response.status_code == 200, response.content
    assert response.json()["export_id"] == "EXP-1"
