"""Client for the customer's legacy ERP.

The integrations in this role fail in boring, repetitive ways: a 429 when you page
too fast, a 503 mid-export, a socket that dies while you are holding a cursor. This
client handles all three explicitly rather than hoping they do not happen.

Policy:

* Honour ``Retry-After`` when the server sends it. Never guess a backoff that is
  shorter than the server asked for.
* Retry 429 and 5xx with exponential backoff and jitter. Cap total attempts.
* Do not retry 4xx that a retry cannot fix (400, 401, 403, 404, 422). A retry loop
  around an authentication failure is how a customer gets rate limited out of their
  own system.
* Every response is parsed defensively. A field the customer's ERP started sending
  last Tuesday must not take the migration down.
* The caller receives ``LegacyPage`` records and never a raw response, so retry and
  pagination semantics live in exactly one place.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class LegacyApiError(RuntimeError):
    """Raised when the legacy system cannot be read after the retry policy is spent."""

    def __init__(self, message: str, *, status: int | None = None, attempts: int = 0):
        super().__init__(message)
        self.status = status
        self.attempts = attempts


@dataclass
class LegacyPage:
    items: list[dict[str, Any]]
    page: int
    total_pages: int
    total_items: int
    raw_status: int = 200
    attempts: int = 1
    rate_limit_waits: int = 0


@dataclass
class RetryStats:
    """Observed behaviour, reported back so the demo can show it rather than claim it."""

    requests: int = 0
    retries: int = 0
    rate_limit_waits: int = 0
    backoff_seconds: float = 0.0
    statuses: dict[str, int] = field(default_factory=dict)

    def record(self, status: int) -> None:
        key = str(status)
        self.statuses[key] = self.statuses.get(key, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "retries": self.retries,
            "rate_limit_waits": self.rate_limit_waits,
            "backoff_seconds": round(self.backoff_seconds, 3),
            "statuses": dict(self.statuses),
        }


class LegacyApiClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        max_attempts: int = 6,
        timeout: float = 15.0,
        session: requests.Session | None = None,
    ):
        self.base_url = (base_url or settings.LEGACY_API_BASE).rstrip("/")
        self.token = token or settings.LEGACY_API_TOKEN
        self.max_attempts = max_attempts
        self.timeout = timeout
        self.session = session or requests.Session()
        self.stats = RetryStats()

    # -- transport ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": "ledger-bridge/1.0 (+migration)",
        }

    def _sleep_for(self, attempt: int, retry_after: str | None) -> float:
        """Full jitter exponential backoff, floored by any Retry-After the server sent."""
        base = min(2 ** (attempt - 1), 20)
        delay = random.uniform(0, base)
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except (TypeError, ValueError):
                pass
        return min(delay, 60.0)

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> tuple[dict[str, Any], int, int]:
        """GET a JSON document. Returns (body, http status, attempts used)."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        last_error: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            self.stats.requests += 1
            try:
                response = self.session.get(url, params=params, headers=self._headers(), timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
                self.stats.retries += 1
                delay = self._sleep_for(attempt, None)
                self.stats.backoff_seconds += delay
                logger.warning("legacy api transport error attempt=%s url=%s error=%s", attempt, url, exc)
                time.sleep(delay)
                continue

            self.stats.record(response.status_code)

            if response.status_code == 200:
                return response.json(), 200, attempt

            if response.status_code in RETRYABLE_STATUS:
                last_error = LegacyApiError(
                    f"{response.status_code} from {url}", status=response.status_code, attempts=attempt
                )
                retry_after = response.headers.get("Retry-After")
                if response.status_code == 429:
                    self.stats.rate_limit_waits += 1
                self.stats.retries += 1
                delay = self._sleep_for(attempt, retry_after)
                self.stats.backoff_seconds += delay
                logger.warning(
                    "legacy api retryable status=%s attempt=%s sleeps=%.2fs url=%s",
                    response.status_code,
                    attempt,
                    delay,
                    url,
                )
                time.sleep(delay)
                continue

            # Non retryable. Fail fast and say why; a loop here is worse than an error.
            raise LegacyApiError(
                f"legacy api returned {response.status_code} for {url}: {response.text[:300]}",
                status=response.status_code,
                attempts=attempt,
            )

        raise LegacyApiError(
            f"legacy api unreachable after {self.max_attempts} attempts: {last_error}",
            attempts=self.max_attempts,
        )

    # -- endpoints ---------------------------------------------------------

    def accounts(self, page: int = 1, page_size: int | None = None) -> LegacyPage:
        page_size = page_size or settings.MIGRATION_PAGE_SIZE
        body, status, attempts = self.get_json("/accounts", {"page": page, "page_size": page_size})
        return self._page(body, page, status, attempts)

    def trial_balance(self) -> dict[str, Any]:
        body, _, _ = self.get_json("/trial-balance")
        return body

    def journal_entries(
        self, page: int = 1, page_size: int | None = None, since: str | None = None
    ) -> LegacyPage:
        page_size = page_size or settings.MIGRATION_PAGE_SIZE
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if since:
            params["since"] = since
        body, status, attempts = self.get_json("/journal-entries", params)
        return self._page(body, page, status, attempts)

    def iter_journal_entries(self, page_size: int | None = None, since: str | None = None) -> Iterator[dict]:
        """Walk every page. The caller does not have to know the pagination scheme."""
        page = 1
        total_pages = 1
        while page <= total_pages:
            result = self.journal_entries(page=page, page_size=page_size, since=since)
            total_pages = result.total_pages
            for item in result.items:
                yield item
            page += 1

    @staticmethod
    def _page(body: dict[str, Any], requested_page: int, status: int, attempts: int) -> LegacyPage:
        items = body.get("data")
        if not isinstance(items, list):
            raise LegacyApiError(
                f"legacy api page {requested_page} returned no 'data' array: {list(body)[:8]}",
                status=status,
                attempts=attempts,
            )
        pagination = body.get("pagination") or {}
        return LegacyPage(
            items=items,
            page=int(pagination.get("page", requested_page)),
            total_pages=int(pagination.get("total_pages", 1)),
            total_items=int(pagination.get("total_items", len(items))),
            raw_status=status,
            attempts=attempts,
        )
