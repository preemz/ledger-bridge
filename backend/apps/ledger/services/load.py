"""The load service.

Everything in this module assumes it may be called twice for the same input, and
that the second call must change nothing. That is the property the whole migration
story rests on: an interrupted load has to be safe to resume, and a resumed load
must not double the customer's books.

Two tables are written here:

* ``Account`` from the legacy chart of accounts, upserted on ``external_ref``.
* ``JournalEntry`` and ``JournalLine`` from the legacy journal, upserted on
  ``(source_system, external_ref)`` for the entry and ``(entry, external_ref)``
  for the line.

Choice of upsert over delete-and-reinsert, which is the other common approach:
delete-and-reinsert is not idempotent in the way that matters, because a crash
between the delete and the insert leaves the customer's ledger short of entries.
A row level upsert is atomic per row, and its failure mode is "one row stale"
rather than "four thousand rows missing".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Sequence

from django.db import transaction

from ..models import (
    Account,
    AccountType,
    EntryStatus,
    JournalEntry,
    JournalLine,
)
from .transform import money

logger = logging.getLogger(__name__)

VALID_TYPES = {choice for choice, _ in AccountType.choices}

# The legacy system uses its own spelling for account types. Mapping is explicit so
# an unknown value becomes a validation failure instead of a silently wrong account.
TYPE_ALIASES = {
    "A": "ASSET",
    "ASSET": "ASSET",
    "BANK": "ASSET",
    "L": "LIABILITY",
    "LIABILITY": "LIABILITY",
    "E": "EQUITY",
    "EQUITY": "EQUITY",
    "R": "REVENUE",
    "REVENUE": "REVENUE",
    "INCOME": "REVENUE",
    "X": "EXPENSE",
    "EXPENSE": "EXPENSE",
    "COST_OF_SALES": "EXPENSE",
}


@dataclass
class LoadResult:
    accounts_upserted: int = 0
    accounts_skipped: int = 0
    entries_upserted: int = 0
    entries_skipped: int = 0
    lines_upserted: int = 0
    lines_skipped: int = 0
    batches: int = 0
    replays_detected: int = 0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []

    def as_dict(self) -> dict[str, Any]:
        return {
            "accounts_upserted": self.accounts_upserted,
            "accounts_skipped": self.accounts_skipped,
            "entries_upserted": self.entries_upserted,
            "entries_skipped": self.entries_skipped,
            "lines_upserted": self.lines_upserted,
            "lines_skipped": self.lines_skipped,
            "batches": self.batches,
            "replays_detected": self.replays_detected,
            "errors": list(self.errors),
        }


def normalise_account(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Map one legacy account into Account field values, or None if unusable."""
    ref = str(raw.get("id") or raw.get("code") or "").strip()
    code = str(raw.get("code") or "").strip()
    if not ref or not code:
        return None
    raw_type = str(raw.get("type") or "").strip().upper()
    mapped = TYPE_ALIASES.get(raw_type)
    if mapped not in VALID_TYPES:
        return None
    return {
        "external_ref": ref,
        "code": code,
        "name": str(raw.get("name") or code).strip()[:160],
        "type": mapped,
        "currency": str(raw.get("currency") or "USD").strip()[:3].upper() or "USD",
        "is_active": bool(raw.get("is_active", True)),
    }


def upsert_accounts(rows: Iterable[dict[str, Any]], source_system: str) -> LoadResult:
    """Upsert the chart of accounts. Safe to run any number of times."""
    result = LoadResult()
    batch: list[dict[str, Any]] = []

    def flush() -> None:
        if not batch:
            return
        with transaction.atomic():
            for values in batch:
                values = dict(values, source_system=source_system)
                existing = (
                    Account.objects.filter(external_ref=values["external_ref"]).first()
                    or Account.objects.filter(code=values["code"]).first()
                )
                if existing is None:
                    Account.objects.create(**values)
                    result.accounts_upserted += 1
                else:
                    changed = False
                    for field in ("code", "name", "type", "currency", "is_active", "source_system"):
                        if getattr(existing, field) != values[field]:
                            setattr(existing, field, values[field])
                            changed = True
                    if changed:
                        existing.save()
                        result.accounts_upserted += 1
                    else:
                        result.accounts_skipped += 1
        # Counted inside the flush, not at the call sites. The trailing flush after the
        # loop used to be followed by a guard on the now-empty batch, so a run with fewer
        # rows than one batch reported zero batches.
        result.batches += 1
        batch.clear()

    for raw in rows:
        normalised = normalise_account(raw)
        if normalised is None:
            result.accounts_skipped += 1
            result.errors.append(f"unmappable account: {raw.get('code')!r} type={raw.get('type')!r}")
            continue
        batch.append(normalised)
        if len(batch) >= 200:
            flush()
    flush()
    return result


def upsert_entries(
    entries: Sequence[dict[str, Any]],
    source_system: str,
    accounts: Sequence[Account] | None = None,
) -> LoadResult:
    """Upsert journal entries and their lines.

    Input is a list of already transformed entry dicts:

        {"external_ref": str, "entry_date": date, "memo": str, "status": str,
         "lines": [{"external_ref": str, "account_code": str, "debit": Decimal,
                    "credit": Decimal, "description": str}]}

    ``accounts`` lets a caller pass an already loaded chart of accounts to avoid a query
    per batch. Both indexes are built from the same list, over every account: deriving the
    code index from the source reference index made any account without an external
    reference invisible to the loader, so every line against it raised LookupError.
    """
    result = LoadResult()
    chart = list(accounts) if accounts is not None else list(Account.objects.all())
    account_by_code = {account.code: account for account in chart}
    account_by_ref = {account.external_ref: account for account in chart if account.external_ref}

    for entry in entries:
        lines = entry.get("lines") or []
        if not lines:
            result.entries_skipped += 1
            result.errors.append(f"entry {entry.get('external_ref')} has no lines")
            continue

        total_debit = sum((money(line.get("debit")) for line in lines), Decimal("0"))
        total_credit = sum((money(line.get("credit")) for line in lines), Decimal("0"))
        if total_debit != total_credit:
            # The database trigger would reject this at COMMIT. Catching it here turns a
            # transaction failure into a reportable break with the offending entry named.
            result.entries_skipped += 1
            result.errors.append(
                f"entry {entry.get('external_ref')} unbalanced: debit {total_debit} credit {total_credit}"
            )
            continue

        entry_created = False
        lines_created = 0
        lines_skipped = 0

        try:
            with transaction.atomic():
                existing = JournalEntry.objects.filter(
                    source_system=source_system, external_ref=entry["external_ref"]
                ).first()
                if existing is None:
                    journal_entry = JournalEntry.objects.create(
                        source_system=source_system,
                        external_ref=entry["external_ref"],
                        entry_date=entry["entry_date"],
                        memo=entry.get("memo", "")[:400],
                        status=entry.get("status", EntryStatus.POSTED),
                        currency=entry.get("currency", "USD"),
                        fx_rate=entry.get("fx_rate", Decimal("1")),
                    )
                    entry_created = True
                else:
                    journal_entry = existing

                # A line with no source identifier cannot be matched on replay, so the
                # whole entry's lines are replaced once, up front. Deleting inside the
                # loop below wiped the lines written by the previous iteration, so an
                # entry with two ref-less lines kept only the last one and the deferred
                # balance trigger then rejected the commit.
                line_refs = [str(line.get("external_ref") or "") for line in lines]
                if not all(line_refs):
                    JournalLine.objects.filter(entry=journal_entry).delete()

                for line, line_ref in zip(lines, line_refs):
                    account = account_by_code.get(line.get("account_code")) or account_by_ref.get(
                        line.get("account_ref", "")
                    )
                    if account is None:
                        raise LookupError(f"account {line.get('account_code')!r} not in chart of accounts")
                    defaults = {
                        "account": account,
                        "debit": money(line.get("debit")),
                        "credit": money(line.get("credit")),
                        "description": str(line.get("description") or "")[:400],
                    }
                    if line_ref:
                        _, created = JournalLine.objects.update_or_create(
                            entry=journal_entry, external_ref=line_ref, defaults=defaults
                        )
                    else:
                        JournalLine.objects.create(entry=journal_entry, external_ref="", **defaults)
                        created = True
                    if created:
                        lines_created += 1
                    else:
                        lines_skipped += 1
        except LookupError as exc:
            result.entries_skipped += 1
            result.errors.append(f"entry {entry.get('external_ref')}: {exc}")
        except Exception as exc:  # noqa: BLE001 - one bad entry must not stop the migration
            result.entries_skipped += 1
            result.errors.append(f"entry {entry.get('external_ref')}: {exc.__class__.__name__}: {exc}")
        else:
            # Counted only once the transaction has committed. Incrementing inside the
            # atomic block meant a rolled back entry was still reported as upserted, so the
            # operator saw a total that included rows the database had discarded.
            if entry_created:
                result.entries_upserted += 1
            else:
                result.entries_skipped += 1
                result.replays_detected += 1
            result.lines_upserted += lines_created
            result.lines_skipped += lines_skipped

    return result
