"""Reconciliation: prove that what we loaded matches what the customer has.

This is the part of a migration that decides whether the finance team signs off, so
it is deliberately conservative and deliberately explicit. It answers three
questions, in this order:

1. Do the totals agree? (``totals``)
2. If not, which accounts disagree and by how much? (``variance_rows``)
3. For each disagreement, what is the most likely cause? (``classify``)

Nothing here trusts an ORM aggregate. The variance query is hand written SQL because
it needs a window function to rank breaks by size and a running total so an operator
can see when the explained breaks stop accounting for the total, and because a
finance reviewer needs to be able to read the query and agree it says what it claims
to say.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.db import connection, transaction
from django.utils import timezone

from ..models import (
    Account,
    JournalEntry,
    MigrationJob,
    ReconciliationBreak,
    ReconciliationRun,
)
from .util import audit, q4

logger = logging.getLogger(__name__)

# Anything under half a cent is a storage artefact, not a business difference.
ROUNDING_TOLERANCE = Decimal("0.005")
# Below this a difference is worth listing but not worth escalating.
REPORT_TOLERANCE = Decimal("0.0001")

VARIANCE_SQL = """
WITH posted_lines AS (
    SELECT jl.account_id, jl.debit, jl.credit
    FROM ledger_journalline jl
    JOIN ledger_journalentry e ON e.id = jl.entry_id
    WHERE e.status = 'POSTED' AND e.entry_date <= %(as_of)s
),
loaded AS (
    SELECT a.id AS account_id,
           a.code,
           a.name,
           a.type,
           a.normal_balance,
           a.currency,
           COALESCE(SUM(pl.debit), 0)::numeric(20,4)  AS loaded_debit,
           COALESCE(SUM(pl.credit), 0)::numeric(20,4) AS loaded_credit
    FROM ledger_account a
    LEFT JOIN posted_lines pl ON pl.account_id = a.id
    GROUP BY a.id, a.code, a.name, a.type, a.normal_balance, a.currency
),
legacy AS (
    SELECT * FROM unnest(
        %(codes)s::text[],
        %(names)s::text[],
        %(balances)s::numeric[]
    ) AS t(code, name, balance)
),
variance AS (
    SELECT
        COALESCE(leg.code, l.code)                              AS account_code,
        COALESCE(leg.name, l.name, '')                          AS account_name,
        l.type                                                  AS account_type,
        l.currency                                              AS currency,
        COALESCE(leg.balance, 0)::numeric(20,4)                 AS legacy_balance,
        (COALESCE(l.loaded_debit, 0) - COALESCE(l.loaded_credit, 0))::numeric(20,4)
                                                                AS loaded_balance,
        ((COALESCE(l.loaded_debit, 0) - COALESCE(l.loaded_credit, 0))
            - COALESCE(leg.balance, 0))::numeric(20,4)          AS variance,
        (leg.code IS NULL)                                      AS missing_from_legacy,
        (l.account_id IS NULL)                                  AS missing_from_ledger
    FROM legacy leg
    FULL OUTER JOIN loaded l ON l.code = leg.code
)
SELECT
    account_code,
    account_name,
    account_type,
    currency,
    legacy_balance,
    loaded_balance,
    variance,
    missing_from_legacy,
    missing_from_ledger,
    RANK() OVER (ORDER BY ABS(variance) DESC) AS variance_rank,
    ROUND(
        100.0 * SUM(ABS(variance)) OVER (
            ORDER BY ABS(variance) DESC
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) / NULLIF(SUM(ABS(variance)) OVER (), 0),
        2
    ) AS cumulative_variance_pct,
    COUNT(*) OVER () AS account_count
FROM variance
WHERE ABS(variance) > %(tolerance)s
ORDER BY variance_rank, account_code
"""

TOTALS_SQL = """
WITH posted_lines AS (
    SELECT jl.debit, jl.credit, jl.account_id, jl.entry_id
    FROM ledger_journalline jl
    JOIN ledger_journalentry e ON e.id = jl.entry_id
    WHERE e.status = 'POSTED' AND e.entry_date <= %(as_of)s
),
per_account AS (
    SELECT a.id AS account_id,
           COALESCE(SUM(pl.debit), 0) - COALESCE(SUM(pl.credit), 0) AS net_debit
    FROM ledger_account a
    LEFT JOIN posted_lines pl ON pl.account_id = a.id
    GROUP BY a.id
),
per_entry AS (
    SELECT entry_id, SUM(debit) AS debit, SUM(credit) AS credit
    FROM posted_lines
    GROUP BY entry_id
)
-- The trial balance convention, on both sides: debits are the positive net balances and
-- credits are the negative net balances. Summing all debit movement instead would mean
-- this number and the customer's number are different quantities, and reporting the
-- difference between two different quantities as a discrepancy is worse than reporting
-- nothing. The reconciliation is only as trustworthy as the definition of its terms.
SELECT
    (SELECT COALESCE(SUM(CASE WHEN net_debit > 0 THEN net_debit ELSE 0 END), 0)
       FROM per_account)::numeric(20,4)  AS loaded_debit,
    (SELECT COALESCE(-SUM(CASE WHEN net_debit < 0 THEN net_debit ELSE 0 END), 0)
       FROM per_account)::numeric(20,4)  AS loaded_credit,
    (SELECT COUNT(*) FROM per_entry)     AS loaded_entry_count,
    (SELECT COUNT(*) FROM posted_lines)  AS loaded_line_count,
    (SELECT COUNT(*) FROM per_entry WHERE debit <> credit) AS unbalanced_entry_count
"""


@dataclass
class LegacyTrialBalance:
    codes: list[str]
    names: list[str]
    balances: list[Decimal]
    total_debit: Decimal
    total_credit: Decimal
    entry_count: int

    def by_code(self) -> dict[str, Decimal]:
        return dict(zip(self.codes, self.balances))


def refresh_balance_view() -> None:
    """Refresh the materialized balance view.

    REFRESH MATERIALIZED VIEW CONCURRENTLY cannot run inside a transaction block, so
    the connection is briefly flipped to autocommit. The unique index on account_id
    is what makes the concurrent form legal, and it is why the refresh does not
    block the operator console while a migration is running.
    """
    with connection.cursor() as cursor:
        cursor.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY ledger_account_balance")
    connection.commit()


def _columns(cursor) -> list[str]:
    return [col[0] for col in cursor.description]


def variance_rows(as_of, legacy: LegacyTrialBalance) -> list[dict[str, Any]]:
    """Per account differences between the loaded ledger and the legacy trial balance."""
    if not legacy.codes:
        return []
    params = {
        "as_of": as_of,
        "tolerance": str(REPORT_TOLERANCE),
        "codes": legacy.codes,
        "names": legacy.names,
        "balances": [str(b) for b in legacy.balances],
    }
    with connection.cursor() as cursor:
        cursor.execute(VARIANCE_SQL, params)
        columns = _columns(cursor)
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def loaded_totals(as_of) -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute(TOTALS_SQL, {"as_of": as_of})
        columns = _columns(cursor)
        row = cursor.fetchone()
    data = dict(zip(columns, row)) if row else {}
    data.setdefault("loaded_debit", Decimal("0"))
    data.setdefault("loaded_credit", Decimal("0"))
    data.setdefault("loaded_entry_count", 0)
    data.setdefault("loaded_line_count", 0)
    data.setdefault("unbalanced_entry_count", 0)
    return data


def classify(
    row: dict[str, Any],
    *,
    as_of,
    late_entry_accounts: set[str],
) -> tuple[str, str, str]:
    """Name the most likely cause of one break.

    Returns (classification, severity, reasoning). The reasoning string is stored on
    the break and shown to the operator, because "TRUE_BREAK" with no explanation is
    not something a finance team can act on.
    """
    variance = abs(Decimal(row["variance"]))

    if row.get("missing_from_ledger"):
        return (
            "MAPPING_ERROR",
            "HIGH",
            "Account exists in the legacy trial balance and has no counterpart in the "
            "chart of accounts that was loaded. Check the account mapping before signing off.",
        )

    if row.get("missing_from_legacy"):
        return (
            "MAPPING_ERROR",
            "MEDIUM",
            "Account was loaded into the ledger but is absent from the legacy trial "
            "balance. Usually a chart of accounts row that was retired in the source system.",
        )

    if variance < ROUNDING_TOLERANCE:
        return (
            "ROUNDING",
            "LOW",
            f"Difference of {variance} is below half a cent and is a storage artefact of "
            "four decimal place money columns.",
        )

    if row.get("currency") and str(row["currency"]).upper() != "USD":
        return (
            "FX",
            "MEDIUM",
            f"Non USD account in {row['currency']}. A residual difference on a foreign "
            "currency balance is normally the rate applied on translation.",
        )

    if row["account_code"] in late_entry_accounts:
        return (
            "TIMING",
            "MEDIUM",
            f"Account has journal activity dated after {as_of}, which is excluded from "
            "this comparison. Confirm the cutover date before treating this as a real break.",
        )

    return (
        "TRUE_BREAK",
        "HIGH",
        f"Variance of {variance} is unexplained by rounding, currency or cutover date. "
        "This needs a human review of the source entries for this account.",
    )


def late_entry_accounts(as_of) -> set[str]:
    """Codes of accounts that have posted activity after the reconciliation date."""
    rows = (
        JournalEntry.objects.filter(status="POSTED", entry_date__gt=as_of)
        .values_list("lines__account__code", flat=True)
        .distinct()
    )
    return {code for code in rows if code}


def run_reconciliation(job: MigrationJob, legacy: LegacyTrialBalance) -> ReconciliationRun:
    """Compare, persist the result, and return the run.

    Re-running replaces the previous run for the same job and cutover date rather than
    appending to it. A Temporal retry of the reconcile activity would otherwise leave
    several runs, each holding a full copy of every break, and the console would show
    whichever one the ordering happened to surface. One job and one cutover date is one
    comparison, and a retry of it must be indistinguishable from the first attempt.
    """
    started = time.perf_counter()
    as_of = job.as_of_date

    with transaction.atomic():
        totals = loaded_totals(as_of)
        rows = variance_rows(as_of, legacy)
        late = late_entry_accounts(as_of)

        # Break rows cascade from the run, so this clears the previous set with it.
        ReconciliationRun.objects.filter(job=job, as_of_date=as_of).delete()

        run = ReconciliationRun.objects.create(
            job=job,
            as_of_date=as_of,
            status="COMPLETED",
            legacy_total_debit=q4(legacy.total_debit),
            legacy_total_credit=q4(legacy.total_credit),
            loaded_total_debit=q4(totals["loaded_debit"]),
            loaded_total_credit=q4(totals["loaded_credit"]),
            legacy_entry_count=int(legacy.entry_count),
            loaded_entry_count=int(totals["loaded_entry_count"]),
            difference=q4(Decimal(totals["loaded_debit"]) - Decimal(legacy.total_debit)),
        )

        breaks = []
        for row in rows:
            classification, severity, reasoning = classify(
                row, as_of=as_of, late_entry_accounts=late
            )
            breaks.append(
                ReconciliationBreak(
                    run=run,
                    account_code=str(row["account_code"])[:32],
                    account_name=str(row["account_name"] or "")[:160],
                    legacy_balance=q4(row["legacy_balance"]),
                    loaded_balance=q4(row["loaded_balance"]),
                    variance=q4(row["variance"]),
                    variance_rank=int(row["variance_rank"]),
                    cumulative_variance_pct=Decimal(str(row["cumulative_variance_pct"])),
                    classification=classification,
                    severity=severity,
                    note=reasoning[:400],
                )
            )
        ReconciliationBreak.objects.bulk_create(breaks)
        run.duration_ms = int((time.perf_counter() - started) * 1000)
        run.save(update_fields=["duration_ms", "updated_at"])

    by_classification: dict[str, int] = {}
    for brk in breaks:
        by_classification[brk.classification] = by_classification.get(brk.classification, 0) + 1

    audit(
        job,
        "reconciliation.completed",
        f"compared {len(legacy.codes)} accounts, {len(breaks)} breaks",
        accounts_compared=len(legacy.codes),
        breaks=len(breaks),
        by_classification=by_classification,
        unbalanced_entries=int(totals["unbalanced_entry_count"]),
        loaded_entries=int(totals["loaded_entry_count"]),
        legacy_entries=int(legacy.entry_count),
    )
    return run


def explain_variance_query(as_of=None, legacy: LegacyTrialBalance | None = None) -> str:
    """Return the EXPLAIN ANALYZE plan for the variance query.

    Exposed as a management command so the plan can be pasted into a review rather than
    asserted. Callers should pass the real trial balance: explaining the query against a
    handful of accounts with zero balances, which an earlier version did, produces a plan
    for a query shape that never runs in production and invites the wrong conclusion.

    The indexes that carry this query are ``entry_date_status_idx`` and
    ``line_account_entry_idx``.
    """
    as_of = as_of or timezone.now().date()
    if legacy is None:
        codes = list(Account.objects.order_by("code").values_list("code", flat=True))
        legacy = LegacyTrialBalance(
            codes=codes,
            names=[""] * len(codes),
            balances=[Decimal("0")] * len(codes),
            total_debit=Decimal("0"),
            total_credit=Decimal("0"),
            entry_count=0,
        )
    params = {
        "as_of": as_of,
        "tolerance": str(REPORT_TOLERANCE),
        "codes": legacy.codes,
        "names": legacy.names,
        "balances": [str(b) for b in legacy.balances],
    }
    with connection.cursor() as cursor:
        cursor.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) " + VARIANCE_SQL, params)
        plan = "\n".join(line[0] for line in cursor.fetchall())
    return plan


def trial_balance_from_api(as_of=None) -> LegacyTrialBalance:
    """Pull the customer's trial balance.

    The legacy system reports a net balance per account in the account's own normal
    direction, so it is converted into a net debit here. Getting this sign convention
    wrong is the single most common way a reconciliation reports thousands of false
    breaks, which is why it happens in one place with a comment.
    """
    from .legacy_client import LegacyApiClient

    client = LegacyApiClient()
    body = client.trial_balance()
    rows = body.get("data") or []
    codes: list[str] = []
    names: list[str] = []
    balances: list[Decimal] = []

    for row in rows:
        code = str(row.get("code") or "").strip()
        if not code:
            continue
        balance = q4(row.get("balance") or 0)
        if str(row.get("normal_balance", "")).upper() == "CREDIT":
            balance = -balance
        codes.append(code)
        names.append(str(row.get("name") or "")[:160])
        balances.append(balance)

    totals = body.get("totals") or {}
    return LegacyTrialBalance(
        codes=codes,
        names=names,
        balances=balances,
        total_debit=q4(totals.get("debit") or sum((b for b in balances if b > 0), Decimal("0"))),
        total_credit=q4(totals.get("credit") or -sum((b for b in balances if b < 0), Decimal("0"))),
        entry_count=int(totals.get("entry_count") or 0),
    )
