"""Run the Temporal worker."""

from __future__ import annotations

import logging

from django.core.management.base import BaseCommand

from apps.migration.worker import main as run_worker_main

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Start a Temporal worker for the ledger-migration task queue. "
        "Kill this process mid-load to demonstrate the crash-resume behaviour."
    )

    def handle(self, *args, **options) -> None:
        try:
            run_worker_main()
        except KeyboardInterrupt:
            self.stdout.write("worker stopped")
        except Exception as exc:  # noqa: BLE001
            self.stderr.write(self.style.ERROR(f"worker failed to start: {exc}"))
            self.stderr.write(
                "Is Temporal running? Try: temporal server start-dev"
            )
            raise SystemExit(1)
