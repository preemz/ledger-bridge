"""Deterministic dataset generator for the simulated legacy ERP.

Produces a chart of accounts, opening balances, a two year journal, some activity after
the cutover date, and a fixed set of deliberate disagreements between the journal and
the trial balance. The anomalies are recorded in ``InjectedAnomaly`` so the test suite
can assert that the reconciliation names the right cause on the right account, rather
than asserting a row count that happens to be right today.

The anomaly accounts are fixed codes rather than drawn from the generated range. That
is deliberate: it means a test can use a 60 account dataset and still get one of every
kind of break, which keeps the suite fast without weakening what it proves.

Everything is seeded, so the same command produces the same books and the same breaks
on any machine. A demo that changes shape every run is a demo you cannot rehearse.
"""

from __future__ import annotations

import datetime as dt
import random
from decimal import Decimal

from django.db import transaction

from .models import (
    AnomalyKind,
    InjectedAnomaly,
    LegacyAccount,
    LegacyDataset,
    LegacyJournalEntry,
    LegacyJournalLine,
)

CENT = Decimal("0.01")
WINDOW_DAYS = 730
CUTOVER_OFFSET_DAYS = 10

# Anchor accounts the generator needs by code, because a payroll entry has to know which
# account is cash.
ANCHORS: list[tuple[str, str, str, str]] = [
    ("1100", "Cash at bank", "ASSET", "USD"),
    ("1101", "Accounts receivable", "ASSET", "USD"),
    ("1150", "Inventory", "ASSET", "USD"),
    ("2100", "Accounts payable", "LIABILITY", "USD"),
    ("2160", "Accumulated depreciation", "LIABILITY", "USD"),
    ("2210", "Payroll taxes payable", "LIABILITY", "USD"),
    ("3001", "Opening balance equity", "EQUITY", "USD"),
    ("3900", "Retained earnings", "EQUITY", "USD"),
    ("4000", "Product revenue", "REVENUE", "USD"),
    ("4010", "Services revenue", "REVENUE", "USD"),
    ("4020", "Subscription revenue", "REVENUE", "USD"),
    ("6000", "Cost of sales", "EXPENSE", "USD"),
    ("6100", "Salaries and wages", "EXPENSE", "USD"),
    ("6200", "Rent", "EXPENSE", "USD"),
    ("6300", "Marketing", "EXPENSE", "USD"),
    ("6400", "Software and subscriptions", "EXPENSE", "USD"),
    ("6500", "Travel and entertainment", "EXPENSE", "USD"),
    ("6600", "Professional fees", "EXPENSE", "USD"),
    ("6700", "Depreciation", "EXPENSE", "USD"),
    ("9999", "Suspense", "EXPENSE", "USD"),
]

# Fixed targets for each injected disagreement.
EUR_CODES = ["1240", "1241", "1242"]
ROUNDING_CODES = ["1250", "1251", "1252"]
TIMING_CODES = ["1260", "1261", "1262"]
TRUE_BREAK_CODES = ["1270", "1271"]
RETIRED_CODE = "1280"

ANOMALY_ACCOUNTS: list[tuple[str, str, str]] = (
    [(code, f"Euro operating account {index + 1}", "EUR") for index, code in enumerate(EUR_CODES)]
    + [(code, f"Cost allocation pool {index + 1}", "USD") for index, code in enumerate(ROUNDING_CODES)]
    + [(code, f"Post cutover clearing {index + 1}", "USD") for index, code in enumerate(TIMING_CODES)]
    + [(code, f"Unreconciled difference pool {index + 1}", "USD") for index, code in enumerate(TRUE_BREAK_CODES)]
    + [(RETIRED_CODE, "Discontinued operation", "USD")]
)

FAMILY_LABELS: dict[str, tuple[str, list[str]]] = {
    "1": ("ASSET", ["Plant and equipment", "Prepaid expenses", "VAT receivable", "Deposits paid", "Work in progress"]),
    "2": ("LIABILITY", ["Accrued expenses", "Deferred revenue", "VAT payable", "Bank loan", "Lease liability"]),
    "3": ("EQUITY", ["Share capital", "Share premium", "Revaluation reserve", "Dividend reserve", "Retained earnings b/f"]),
    "4": ("REVENUE", ["Training revenue", "Support revenue", "Implementation revenue", "Licence revenue", "Usage revenue"]),
    "5": ("EXPENSE", ["Contractor costs", "Insurance", "Utilities", "Recruitment", "Bank charges"]),
}

DEBIT_NORMAL = {"ASSET", "EXPENSE"}


def _four_dp(rnd: random.Random) -> Decimal:
    """An amount with a sub-cent fraction, the way a unit price times a quantity lands.

    Kept under half a cent so an ERP which displays two decimal places produces a
    difference small enough to be rounding and nothing worse.
    """
    return Decimal(f"{rnd.randint(200, 5000)}.{rnd.randint(1, 49):04d}")


def _clear() -> None:
    LegacyJournalLine.objects.all().delete()
    LegacyJournalEntry.objects.all().delete()
    LegacyAccount.objects.all().delete()
    InjectedAnomaly.objects.all().delete()
    LegacyDataset.objects.all().delete()


def _build_chart(account_target: int) -> dict[str, LegacyAccount]:
    rows: list[LegacyAccount] = [
        LegacyAccount(
            code=code,
            name=name,
            type=type_,
            normal_balance="DEBIT" if type_ in DEBIT_NORMAL else "CREDIT",
            currency=currency,
        )
        for code, name, type_, currency in ANCHORS
    ]
    rows += [
        LegacyAccount(
            code=code,
            name=name,
            type="ASSET",
            normal_balance="DEBIT",
            currency=currency,
        )
        for code, name, currency in ANOMALY_ACCOUNTS
    ]

    remaining = max(0, account_target - len(rows))
    per_family = (remaining + 4) // 5
    for family, (type_, labels) in FAMILY_LABELS.items():
        for index in range(per_family):
            if len(rows) >= account_target:
                break
            label = labels[index % len(labels)]
            suffix = "" if index < len(labels) else f" {index // len(labels) + 1}"
            rows.append(
                LegacyAccount(
                    code=f"{family}{300 + index:03d}",
                    name=f"{label}{suffix}",
                    type=type_,
                    normal_balance="DEBIT" if type_ in DEBIT_NORMAL else "CREDIT",
                    currency="USD",
                )
            )
        if len(rows) >= account_target:
            break

    LegacyAccount.objects.bulk_create(rows)
    return {a.code: a for a in LegacyAccount.objects.all()}


def _openings(accounts: dict[str, LegacyAccount], rnd: random.Random) -> list[tuple]:
    """Opening balances, as the source system actually exports them: journal entries.

    There is no separate opening balance column to add on top. The trial balance is
    movement plus openings, and the openings are movement, so adding a stored opening
    figure as well would count every balance twice and inflate the whole reconciliation
    by the size of the customer's balance sheet. The first version of this did exactly
    that, which is why the reconciliation test asserts on a specific account rather than
    on a total.
    """
    equity = accounts["3001"]
    dated = dt.date.today() - dt.timedelta(days=WINDOW_DAYS + 1)
    entries: list[tuple] = []

    for account in accounts.values():
        if account.type in {"REVENUE", "EXPENSE"} or account.code in {"3001", "9999"}:
            continue
        magnitude = Decimal(rnd.randint(20_000, 900_000))
        if account.normal_balance == "DEBIT":
            lines = [
                (account, magnitude, Decimal("0"), "Opening balance"),
                (equity, Decimal("0"), magnitude, "Opening balance equity"),
            ]
        else:
            lines = [
                (account, Decimal("0"), magnitude, "Opening balance"),
                (equity, magnitude, Decimal("0"), "Opening balance equity"),
            ]
        entries.append((f"OB-{account.code}", dated, f"Opening balance {account.code}", lines))

    return entries


def _period_entries(
    accounts: dict[str, LegacyAccount],
    count: int,
    rnd: random.Random,
    reserved: set[str],
) -> list[tuple]:
    window_start = dt.date.today() - dt.timedelta(days=WINDOW_DAYS)
    eligible = [a for a in accounts.values() if a.code not in reserved]

    def pick(type_: str) -> LegacyAccount:
        return rnd.choice([a for a in eligible if a.type == type_])

    entries: list[tuple] = []
    for index in range(count):
        ref = f"JE-{index + 1:06d}"
        day = window_start + dt.timedelta(days=rnd.randint(0, WINDOW_DAYS - CUTOVER_OFFSET_DAYS - 2))
        amount = Decimal(rnd.randint(500, 250_000)) + Decimal(rnd.randint(0, 99)) / 100
        roll = rnd.random()

        if roll < 0.30:
            counterparty = accounts["1101"] if rnd.random() < 0.6 else accounts["1100"]
            lines = [
                (counterparty, amount, Decimal("0"), "Customer invoice"),
                (pick("REVENUE"), Decimal("0"), amount, "Revenue recognised"),
            ]
            memo = "Sales invoice"
        elif roll < 0.60:
            counterparty = accounts["2100"] if rnd.random() < 0.7 else accounts["1100"]
            lines = [
                (pick("EXPENSE"), amount, Decimal("0"), "Supplier invoice"),
                (counterparty, Decimal("0"), amount, "Amount payable"),
            ]
            memo = "Supplier invoice"
        elif roll < 0.75:
            lines = [
                (accounts["1100"], amount, Decimal("0"), "Receipt"),
                (accounts["1101"], Decimal("0"), amount, "Settled invoice"),
            ]
            memo = "Customer receipt"
        elif roll < 0.90:
            lines = [
                (accounts["2100"], amount, Decimal("0"), "Supplier payment"),
                (accounts["1100"], Decimal("0"), amount, "Payment"),
            ]
            memo = "Supplier payment"
        elif roll < 0.95:
            tax = (amount * Decimal("0.12")).quantize(CENT)
            lines = [
                (accounts["6100"], amount, Decimal("0"), "Payroll"),
                (accounts["1100"], Decimal("0"), amount - tax, "Net pay"),
                (accounts["2210"], Decimal("0"), tax, "Payroll taxes"),
            ]
            memo = "Payroll run"
        else:
            lines = [
                (accounts["6700"], amount, Decimal("0"), "Depreciation charge"),
                (accounts["2160"], Decimal("0"), amount, "Accumulated depreciation"),
            ]
            memo = "Depreciation"

        entries.append((ref, day, memo, lines))

    return entries


def _late_entries(accounts: dict[str, LegacyAccount], rnd: random.Random) -> list[tuple]:
    """Activity after the cutover date, which an as-of comparison will not see."""
    cutover = dt.date.today() - dt.timedelta(days=CUTOVER_OFFSET_DAYS)
    entries: list[tuple] = []
    for index, code in enumerate(TIMING_CODES):
        account = accounts[code]
        for step in range(3):
            day = min(cutover + dt.timedelta(days=1 + step * 3), dt.date.today())
            amount = Decimal(rnd.randint(1_500, 40_000)) + Decimal(rnd.randint(0, 99)) / 100
            lines = [
                (account, amount, Decimal("0"), "Post cutover activity"),
                (accounts["1100"], Decimal("0"), amount, "Post cutover activity"),
            ]
            entries.append((f"LATE-{index + 1:03d}-{step + 1}", day, "Activity after cutover", lines))
    return entries


def _distortion_entries(accounts: dict[str, LegacyAccount], rnd: random.Random) -> list[tuple]:
    """Balanced entries that give the rounding accounts a sub-cent fraction."""
    entries: list[tuple] = []
    for index, code in enumerate(ROUNDING_CODES):
        amount = _four_dp(rnd)
        lines = [
            (accounts[code], amount, Decimal("0"), "Cost allocation"),
            (accounts["9999"], Decimal("0"), amount, "Cost allocation"),
        ]
        entries.append(
            (
                f"ADJ-{index + 1:04d}",
                dt.date.today() - dt.timedelta(days=200 + index),
                "Allocation",
                lines,
            )
        )
    return entries


def _write_journal(entries: list[tuple]) -> None:
    journal_rows: list[LegacyJournalEntry] = []
    line_specs: list[tuple[str, int, LegacyAccount, Decimal, Decimal, str]] = []

    for ref, day, memo, lines in entries:
        debit_total = sum((line[1] for line in lines), Decimal("0"))
        credit_total = sum((line[2] for line in lines), Decimal("0"))
        if debit_total != credit_total:
            raise AssertionError(
                f"generator produced an unbalanced entry {ref}: {debit_total} vs {credit_total}"
            )
        journal_rows.append(LegacyJournalEntry(ref=ref, entry_date=day, memo=memo))
        for position, (account, debit, credit, description) in enumerate(lines, start=1):
            line_specs.append((ref, position, account, debit, credit, description))

    LegacyJournalEntry.objects.bulk_create(journal_rows)
    entry_by_ref = {e.ref: e for e in LegacyJournalEntry.objects.all()}
    LegacyJournalLine.objects.bulk_create(
        [
            LegacyJournalLine(
                entry=entry_by_ref[ref],
                account=account,
                ref=f"{ref}-L{position}",
                debit=debit,
                credit=credit,
                description=(description or f"{ref}-{position}")[:400],
            )
            for ref, position, account, debit, credit, description in line_specs
        ]
    )


def _record_anomalies() -> list[InjectedAnomaly]:
    return [
        *[
            InjectedAnomaly(
                kind=AnomalyKind.ROUNDING,
                account_code=code,
                detail="Trial balance reports the balance rounded to the display precision of the ERP",
            )
            for code in ROUNDING_CODES
        ],
        *[
            InjectedAnomaly(
                kind=AnomalyKind.FX,
                account_code=code,
                detail="Foreign currency balance carries a translation residual",
            )
            for code in EUR_CODES
        ],
        *[
            InjectedAnomaly(
                kind=AnomalyKind.TIMING,
                account_code=code,
                detail="All activity on this account is dated after the cutover date",
            )
            for code in TIMING_CODES
        ],
        *[
            InjectedAnomaly(
                kind=AnomalyKind.TRUE_BREAK,
                account_code=code,
                detail="Unexplained difference between the journal and the reported balance",
                magnitude=Decimal("12500.00") if index == 0 else Decimal("-8750.00"),
            )
            for index, code in enumerate(TRUE_BREAK_CODES)
        ],
        InjectedAnomaly(
            kind=AnomalyKind.MISSING_IN_LEDGER,
            account_code="1410",
            detail="Consultancy clearing account",
            magnitude=Decimal("5000.00"),
        ),
        InjectedAnomaly(
            kind=AnomalyKind.MISSING_IN_LEDGER,
            account_code="1420",
            detail="Grants receivable",
            magnitude=Decimal("2500.00"),
        ),
        InjectedAnomaly(
            kind=AnomalyKind.MISSING_FROM_TRIAL_BALANCE,
            account_code=RETIRED_CODE,
            detail="Account was retired in the source system and no longer appears in the trial balance",
        ),
    ]


@transaction.atomic
def generate(account_target: int = 240, entry_count: int = 4000, seed: int = 20260918) -> LegacyDataset:
    rnd = random.Random(seed)
    _clear()

    accounts = _build_chart(account_target)

    entries: list[tuple] = list(_openings(accounts, rnd))
    entries += _period_entries(accounts, entry_count, rnd, set(TIMING_CODES))
    entries += _late_entries(accounts, rnd)
    entries += _distortion_entries(accounts, rnd)
    _write_journal(entries)

    InjectedAnomaly.objects.bulk_create(_record_anomalies())

    # A retired account is absent from the trial balance but still in the chart export.
    LegacyAccount.objects.filter(code=RETIRED_CODE).update(include_in_trial_balance=False)

    window_end = dt.date.today()
    return LegacyDataset.objects.create(
        seed=seed,
        window_start=window_end - dt.timedelta(days=WINDOW_DAYS),
        window_end=window_end,
        as_of_date=window_end - dt.timedelta(days=CUTOVER_OFFSET_DAYS),
        account_count=LegacyAccount.objects.count(),
        entry_count=LegacyJournalEntry.objects.count(),
    )
