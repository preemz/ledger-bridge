#!/usr/bin/env python
"""Verify the crash-resume claim against a real Temporal server.

Starts a worker, requests a migration, kills the worker with SIGKILL while a LOAD
activity is mid-flight, restarts the worker, and then reads the database to confirm that
the resumed load produced no duplicated journal entries.

This is the claim the project rests on, so it is checkable with one command rather than
taken on faith:

    temporal server start-dev          # terminal 1
    make api                           # terminal 2
    make crash-check                   # terminal 3

Exits non-zero if the worker was never interrupted, if any journal entry was written
twice, or if the run did not reach a terminal status.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
VENV_PY = ROOT / ".venv" / "bin" / "python"
API = os.environ.get("LEDGER_BRIDGE_API", "http://127.0.0.1:8000")
WORKER_LOG = Path("/tmp/lb_worker.log")

# Slows the load deliberately so the kill lands inside an activity rather than between
# them. Has no effect on a normal run.
PACING_MS = os.environ.get("CRASH_CHECK_PACING_MS", "600")

VERIFY_CODE = """
import json
from django.db.models import Count
from apps.ledger.models import JournalEntry, JournalLine, MigrationJob, MigrationPhase
from apps.legacy_sim.models import LegacyJournalEntry

# Grouped on the key the ledger actually enforces uniqueness over. Grouping by
# external_ref alone would miss a duplicate that differed only by source system, which is
# a weaker check than the sentence in the README claims.
duplicates = (
    JournalEntry.objects.values("source_system", "external_ref")
    .annotate(n=Count("id"))
    .filter(n__gt=1)
    .count()
)
null_refs = JournalEntry.objects.filter(external_ref="").count()
job = MigrationJob.objects.order_by("-created_at").first()
print("BEGIN_JSON")
print(json.dumps({
    "ledger_entries": JournalEntry.objects.count(),
    "ledger_lines": JournalLine.objects.count(),
    "source_entries": LegacyJournalEntry.objects.count(),
    "duplicate_source_and_ref_pairs": duplicates,
    "entries_without_a_source_ref": null_refs,
    "job_status": job.status,
    "executor": job.executor,
    "counters": job.counters,
    "attempts_by_phase": {
        p.name: sorted(
            set(MigrationPhase.objects.filter(job=job, name=p.name).values_list("attempt", flat=True))
        )
        for p in MigrationPhase.objects.filter(job=job).order_by("name").distinct("name")
    },
    "phases_left_running": MigrationPhase.objects.filter(job=job, status="RUNNING").count(),
}, indent=2))
print("END_JSON")
"""


def start_worker() -> subprocess.Popen:
    log = WORKER_LOG.open("a")
    log.write("\n=== starting worker ===\n")
    log.flush()
    env = os.environ.copy()
    env["MIGRATION_PACING_MS"] = PACING_MS
    return subprocess.Popen(
        [str(VENV_PY), "manage.py", "run_worker"],
        cwd=str(BACKEND),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def main() -> int:
    worker = start_worker()
    print(f"worker started (pid {worker.pid}), waiting for it to register")
    time.sleep(12)

    accepted = requests.post(
        f"{API}/api/jobs/",
        json={"customer_reference": "Northwind Trading", "source_system": "legacy_erp"},
        timeout=30,
    )
    accepted.raise_for_status()
    job = accepted.json()
    job_id = job["id"]
    executor = job.get("executor")
    print(f"job {job_id} accepted on {executor}, as of {job['as_of_date']}")

    if executor != "temporal":
        print("FAIL: the job did not start on Temporal, so there is nothing to interrupt")
        print(job.get("detail", ""))
        worker.kill()
        return 1

    killed_at: float | None = None
    status = "PENDING"
    deadline = time.time() + 240

    while time.time() < deadline:
        detail = requests.get(f"{API}/api/jobs/{job_id}/", timeout=30).json()
        status = detail["status"]

        # chunk 0 is the chart of accounts; any other chunk is an entry load, which is the
        # interruption that actually tests the upsert.
        entry_load_running = [
            phase
            for phase in detail["phases"]
            if phase["name"] == "LOAD" and phase["status"] == "RUNNING" and phase["chunk"] > 0
        ]

        if entry_load_running and killed_at is None:
            print(f"LOAD is mid-flight (chunk {entry_load_running[-1]['chunk']}), sending SIGKILL")
            worker.kill()
            worker.wait(timeout=15)
            killed_at = time.time()
            time.sleep(4)
            print("the activity heartbeat will now lapse; restarting the worker")
            worker = start_worker()
            time.sleep(10)

        if status in {"COMPLETED", "FAILED", "NEEDS_REVIEW"}:
            break
        time.sleep(0.25)

    if worker.poll() is None:
        worker.kill()

    print(f"terminal status: {status}")
    if killed_at is None:
        print("FAIL: the worker was never interrupted, so nothing was proved")
        return 1
    print(f"the interruption landed {round(time.time() - killed_at, 1)}s into the load")

    verify = subprocess.run(
        [str(VENV_PY), "manage.py", "shell", "-c", VERIFY_CODE],
        cwd=str(BACKEND),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if "BEGIN_JSON" not in verify.stdout:
        print(verify.stdout)
        print(verify.stderr)
        print("FAIL: verification produced no result")
        return 1

    payload = json.loads(verify.stdout.split("BEGIN_JSON", 1)[1].split("END_JSON", 1)[0])
    print("--- after the resumed load ---")
    print(json.dumps(payload, indent=2))

    ok = (
        payload["duplicate_source_and_ref_pairs"] == 0
        and payload["entries_without_a_source_ref"] == 0
        and payload["phases_left_running"] == 0
        and payload["ledger_entries"] > 0
        and payload["ledger_entries"] == payload["source_entries"]
        and status in {"COMPLETED", "NEEDS_REVIEW"}
    )
    print()
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
