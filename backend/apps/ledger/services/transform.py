"""Transform: legacy shape in, ledger shape out, with every refusal explained.

The legacy ERP and the ledger do not agree on much: field names, the sign
convention, whether a line carries a debit or a credit column or one signed amount,
whether the date is a string. This module is the only place that knows about the
legacy shape, which means a second source system is a second function here rather
than a change anywhere else.

A transform never raises on bad data. It returns the normalised entry plus a list of
problems, because a migration has to be able to say "these 37 entries out of 4000 are
unusable and here is why" instead of stopping at the first one.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Any

from ..models import EntryStatus
from .util import q4

VALID_STATUS = {choice for choice, _ in EntryStatus.choices}

DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y", "%Y%m%d")


def parse_date(raw: Any) -> dt.date | None:
    if isinstance(raw, dt.datetime):
        return raw.date()
    if isinstance(raw, dt.date):
        return raw
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in DATE_FORMATS:
        try:
            return dt.datetime.strptime(text[:10] if fmt == "%Y-%m-%d" else text, fmt).date()
        except ValueError:
            continue
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def money(raw: Any) -> Decimal:
    if raw in (None, ""):
        return Decimal("0")
    try:
        return q4(Decimal(str(raw).replace(",", "")))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def normalise_entry(raw: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """Return (normalised entry, problems). Never raises."""
    problems: list[str] = []
    ref = str(raw.get("id") or raw.get("external_ref") or "").strip()
    if not ref:
        return None, ["entry has no identifier"]

    entry_date = parse_date(raw.get("date") or raw.get("entry_date"))
    if entry_date is None:
        problems.append(f"unparseable date {raw.get('date')!r}")
        # Rejected outright, not returned as a normalised entry with a problem attached.
        # An entry with no date cannot be posted to a ledger, and the load skips it, so
        # returning it here meant it counted as validated and then silently disappeared:
        # no rejection count, and the job did not stop for review.
        return None, problems

    raw_status = str(raw.get("status") or "POSTED").strip().upper()
    status = raw_status if raw_status in VALID_STATUS else "POSTED"
    if raw_status not in VALID_STATUS:
        problems.append(f"unknown status {raw_status!r}, treated as POSTED")

    raw_lines = raw.get("lines") or []
    if not isinstance(raw_lines, list) or not raw_lines:
        return None, problems + ["entry has no lines"]

    lines: list[dict[str, Any]] = []
    debit_total = Decimal("0")
    credit_total = Decimal("0")

    for index, raw_line in enumerate(raw_lines):
        account_code = str(raw_line.get("account_code") or raw_line.get("account") or "").strip()
        if not account_code:
            problems.append(f"line {index} has no account code")
            continue

        debit = money(raw_line.get("debit"))
        credit = money(raw_line.get("credit"))

        # Some legacy exports ship a single signed amount instead of two columns.
        if debit == 0 and credit == 0 and raw_line.get("amount") is not None:
            amount = money(raw_line.get("amount"))
            if amount >= 0:
                debit = amount
            else:
                credit = -amount

        if debit > 0 and credit > 0:
            problems.append(f"line {index} has both a debit and a credit, credit discarded")
            credit = Decimal("0")
        if debit == 0 and credit == 0:
            problems.append(f"line {index} is empty, skipped")
            continue

        debit_total += debit
        credit_total += credit
        lines.append(
            {
                "external_ref": str(raw_line.get("id") or f"{ref}-{index + 1}").strip(),
                "account_code": account_code,
                "debit": str(debit),
                "credit": str(credit),
                "description": str(raw_line.get("description") or raw_line.get("memo") or "")[:400],
            }
        )

    if not lines:
        return None, problems + ["every line was unusable"]

    balanced = debit_total == credit_total
    if not balanced:
        problems.append(f"entry does not balance: debit {debit_total} credit {credit_total}")

    normalised = {
        "external_ref": ref,
        "entry_date": entry_date.isoformat() if entry_date else None,
        "memo": str(raw.get("memo") or raw.get("description") or "")[:400],
        "status": status,
        "currency": str(raw.get("currency") or "USD").strip()[:3].upper() or "USD",
        "lines": lines,
        "debit_total": str(debit_total),
        "credit_total": str(credit_total),
        "balanced": balanced,
    }
    return normalised, problems