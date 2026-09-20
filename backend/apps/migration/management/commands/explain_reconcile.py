"""Print the query plan for the reconciliation variance query.

Handy when reviewing this project, and handy on site: a reconciliation that takes
ninety seconds is a reconciliation nobody runs before a cutover.

It explains the query with the customer's real trial balance loaded into it. A plan
taken against a handful of accounts with zero balances describes a query shape that
never runs in production, and an earlier version of this command did exactly that. The
capture in docs/query-plans.md is now the unabridged output of this command.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.management.base import BaseCommand

from apps.ledger.models import Account
from apps.ledger.services.reconcile import LegacyTrialBalance, explain_variance_query
from apps.legacy_sim.models import LegacyDataset
from apps.legacy_sim.reporting import trial_balance


class Command(BaseCommand):
    help = "EXPLAIN ANALYZE the per account variance query used by reconciliation."

    def handle(self, *args, **options) -> None:
        dataset = LegacyDataset.objects.order_by("-seeded_at").first()
        as_of = dataset.as_of_date if dataset else None

        legacy = self._legacy_trial_balance()
        if legacy is None:
            self.stderr.write(
                self.style.WARNING(
                    "No seeded legacy dataset, so the plan is explained against the chart of "
                    "accounts with zero balances. Run `manage.py seed_legacy` for the real shape."
                )
            )

        self.stdout.write(explain_variance_query(as_of=as_of, legacy=legacy))

    @staticmethod
    def _legacy_trial_balance() -> LegacyTrialBalance | None:
        """Build the customer's side of the comparison straight from the simulator.

        Reading the simulator's reporting function rather than calling its HTTP endpoint
        keeps this command usable without a running API server, which is the point of a
        command you run while reviewing a query plan.
        """
        if not Account.objects.exists():
            return None

        body = trial_balance()
        codes: list[str] = []
        names: list[str] = []
        balances: list[Decimal] = []
        for row in body["data"]:
            balance = Decimal(str(row["balance"]))
            if str(row.get("normal_balance", "")).upper() == "CREDIT":
                balance = -balance
            codes.append(str(row["code"]))
            names.append(str(row.get("name") or ""))
            balances.append(balance)

        if not codes:
            return None

        totals = body["totals"]
        return LegacyTrialBalance(
            codes=codes,
            names=names,
            balances=balances,
            total_debit=Decimal(str(totals["debit"])),
            total_credit=Decimal(str(totals["credit"])),
            entry_count=int(totals["entry_count"]),
        )
