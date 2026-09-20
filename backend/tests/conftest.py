"""Test fixtures.

The migration client talks HTTP to the simulated legacy ERP. In tests it talks to
Django's test client instead, through a session adapter, so the suite exercises the
real views, the real pagination, the real serialisation and the real signatures
without needing a listening socket or a fixture server.

Flakiness and rate limiting are switched off here and exercised deliberately, by hand,
in the tests that are about them.
"""

from __future__ import annotations

import json
from urllib.parse import urlencode, urlsplit

import pytest
from django.test import Client, override_settings

from apps.ledger.services import legacy_client
from apps.legacy_sim import ratelimit

LEGACY_TEST_BASE = "http://testserver/legacy-api/v1"


class InProcessResponse:
    def __init__(self, response):
        self._response = response
        self.status_code = response.status_code
        self.headers = response.headers
        self.text = response.content.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self._response.content)


class InProcessSession:
    """Speaks the subset of the requests.Session interface the client uses."""

    def __init__(self) -> None:
        self.client = Client()

    def get(self, url: str, params=None, headers=None, timeout=None) -> InProcessResponse:
        parts = urlsplit(url)
        query = urlencode(params or {})
        path = parts.path + (f"?{query}" if query else "")
        response = self.client.get(path, headers=headers or {})
        return InProcessResponse(response)


@pytest.fixture(autouse=True)
def legacy_settings():
    with override_settings(
        LEGACY_API_BASE=LEGACY_TEST_BASE,
        LEGACY_FLAKE_RATE=0.0,
        LEGACY_RATE_LIMIT_PER_SECOND=0,
        TEMPORAL_ENABLED=False,
    ):
        ratelimit.reset()
        yield
        ratelimit.reset()


@pytest.fixture
def in_process_legacy(monkeypatch):
    """Route the migration client's HTTP calls into Django's test client."""
    original_init = legacy_client.LegacyApiClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs.setdefault("base_url", LEGACY_TEST_BASE)
        original_init(self, *args, **kwargs)
        self.session = InProcessSession()

    monkeypatch.setattr(legacy_client.LegacyApiClient, "__init__", patched_init)
    return InProcessSession


@pytest.fixture
def seeded_legacy(db):
    """A small dataset that still contains one of every injected anomaly."""
    from apps.legacy_sim.generator import generate

    return generate(account_target=80, entry_count=200, seed=4242)
