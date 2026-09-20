"""Generate the customer's books in the simulated legacy ERP."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.legacy_sim.generator import generate
from apps.legacy_sim.models import InjectedAnomaly


class Command(BaseCommand):
    help = "Generate a deterministic legacy ERP dataset, including deliberate anomalies."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--accounts", type=int, default=240)
        parser.add_argument("--entries", type=int, default=4000)
        parser.add_argument("--seed", type=int, default=20260918)

    def handle(self, *args, **options) -> None:
        dataset = generate(
            account_target=options["accounts"],
            entry_count=options["entries"],
            seed=options["seed"],
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"seeded {dataset.account_count} accounts and {dataset.entry_count} journal entries"
            )
        )
        self.stdout.write(
            f"  window        {dataset.window_start} to {dataset.window_end}\n"
            f"  cutover date  {dataset.as_of_date}  (use this as the migration as_of_date)\n"
            f"  seed          {dataset.seed}"
        )
        self.stdout.write("  injected disagreements between the journal and the trial balance:")
        for anomaly in InjectedAnomaly.objects.all():
            self.stdout.write(f"    {anomaly.kind:<28} {anomaly.account_code:<8} {anomaly.detail}")
