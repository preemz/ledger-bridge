"""The trial balance and the journal, as the customer's ERP reports them.

The trial balance is not a straight aggregate of the journal. Read ``trial_balance``
carefully: every distortion applied there corresponds to a row in ``InjectedAnomaly``,
and each one models a real disagreement a migration team meets on site.

* an account whose reported balance is rounded to the display precision of the ERP
* an account holding a translation residual in a foreign currency
* activity after the cutover date that an as-of comparison will not see
* a chart of accounts row the finance team retired, which is absent here
* a trial balance row belonging to an account the export never included
* a difference with no benign explanation at all
"""

from __future__ import annotations

from decimal import Decimal

from django.db.models import Sum

from .models import AnomalyKind, InjectedAnomaly, LegacyAccount, LegacyJournalEntry, LegacyJournalLine

CENT = Decimal("0.01")
FX_TRANSLATION_RESIDUAL = Decimal("1.0007")

PLACES = Decimal("0.0001")


def movement_by_account() -> dict[int, tuple[Decimal, Decimal]]:
    """Net journal movement per account, in one aggregate query."""
    rows = (
        LegacyJournalLine.objects.values("account_id")
        .annotate(debit=Sum("debit"), credit=Sum("credit"))
    )
    return {
        row["account_id"]: (row["debit"] or Decimal("0"), row["credit"] or Decimal("0"))
        for row in rows
    }


def trial_balance() -> dict:
    """The customer's trial balance as their ERP would export it."""
    accounts = list(LegacyAccount.objects.all().order_by("code"))
    movement = movement_by_account()

    grouped: dict[str, list[InjectedAnomaly]] = {}
    for anomaly in InjectedAnomaly.objects.all():
        grouped.setdefault(str(anomaly.kind), []).append(anomaly)

    rounding_codes = {a.account_code for a in grouped.get("ROUNDING", [])}
    fx_codes = {a.account_code for a in grouped.get("FX", [])}
    true_break = {a.account_code: a.magnitude for a in grouped.get("TRUE_BREAK", [])}
    hidden = {a.account_code for a in grouped.get("MISSING_FROM_TRIAL_BALANCE", [])}

    data: list[dict] = []
    total_debit = Decimal("0")
    total_credit = Decimal("0")

    for account in accounts:
        if account.code in hidden or not account.include_in_trial_balance:
            continue

        debit, credit = movement.get(account.id, (Decimal("0"), Decimal("0")))
        net = debit - credit

        if account.code in rounding_codes:
            # The ERP displays two decimal places and exports what it displays.
            net = net.quantize(CENT)
        if account.code in fx_codes:
            net = (net * FX_TRANSLATION_RESIDUAL).quantize(PLACES)
        if account.code in true_break:
            net = net + true_break[account.code]

        # The ERP reports each account in its own normal direction.
        balance = net if account.normal_balance == "DEBIT" else -net
        data.append(
            {
                "code": account.code,
                "name": account.name,
                "type": account.type,
                "normal_balance": account.normal_balance,
                "currency": account.currency,
                "balance": str(balance.quantize(PLACES)),
            }
        )
        if net >= 0:
            total_debit += net
        else:
            total_credit += -net

    # Trial balance rows with no counterpart in the chart of accounts export.
    for anomaly in grouped.get("MISSING_IN_LEDGER", []):
        data.append(
            {
                "code": anomaly.account_code,
                "name": anomaly.detail or anomaly.account_code,
                "type": "EXPENSE",
                "normal_balance": "DEBIT",
                "currency": "USD",
                "balance": str(anomaly.magnitude),
            }
        )
        total_debit += anomaly.magnitude

    return {
        "data": sorted(data, key=lambda row: row["code"]),
        "totals": {
            "debit": str(total_debit.quantize(PLACES)),
            "credit": str(total_credit.quantize(PLACES)),
            "entry_count": LegacyJournalEntry.objects.count(),
        },
    }


__all__ = ["AnomalyKind", "movement_by_account", "trial_balance"]
