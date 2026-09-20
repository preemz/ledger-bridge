# Ledger Bridge

An accounting data migration and reconciliation service.

A customer's finance team is leaving a legacy ERP. Ledger Bridge extracts their
chart of accounts, opening balances and journal entries, loads them into a
double-entry ledger, then reconciles the loaded trial balance against the legacy
one and reports every break it finds.

Built to the shape of a forward-deployed engineering role: own a customer project
from discovery through a verified production release, with durable workflows,
idempotent and resumable loads, and reconciliation that a finance team can sign off on.

## Why these pieces exist

| Requirement | Where it lives |
| --- | --- |
| Django, DRF | `backend/apps/ledger/`, `backend/config/` |
| Advanced PostgreSQL | deferred constraint trigger enforcing double entry, append-only audit trigger, materialized balance view, window-function variance query (`backend/apps/ledger/migrations/0002_integrity.py`, `backend/apps/ledger/services/reconcile.py`) |
| Idempotent, resumable, auditable workflows | `backend/apps/migration/workflows.py`, `backend/apps/migration/activities.py` |
| Temporal (durable execution) | same, plus `backend/apps/migration/worker.py` |
| Third-party APIs, rate limits, retries, failure recovery | `backend/apps/ledger/services/legacy_client.py` against the rate limited, deliberately flaky `backend/apps/legacy_sim/` service |
| Data migration, reconciliation, conflicting systems | `backend/apps/ledger/services/load.py`, `reconcile.py` |
| Next.js, React, TypeScript | `frontend/` |
| Customer-facing delivery | `docs/design-note.md`, `docs/demo-runbook.md` |

## Quick start

Requirements: Python 3.11+, PostgreSQL 14+, Node 20+, and the Temporal CLI.

```bash
brew install postgresql@16 temporal
brew services start postgresql@16

createdb ledger_bridge
make install     # python venv + npm
make migrate     # schema, including the integrity triggers
make seed        # generates a legacy ERP export with known anomalies

make api         # http://localhost:8000
make worker      # Temporal worker, separate terminal
make temporal    # Temporal dev server, separate terminal
make web         # http://localhost:3000
```

The seed writes a legacy dataset into the simulator app. Nothing is loaded into the
ledger until you start a migration from the UI or the API.

## Verified, not asserted

Everything below was executed against PostgreSQL 16.15 and Temporal 1.33 on the machine
this was built on, and the numbers are the ones that came back.

* **43 tests pass** (`make test`). They cover the deferred double entry trigger, the
  append-only audit trigger, idempotent loading, replayed activity writes, multi window
  pagination, validation failing for the right reason, the reconciliation classifier
  against every deliberately injected anomaly, break ranking, audit log signal against
  noise, and the API contract the console is written against.
* **Crash and resume** (`make crash-check`, or `scripts/crash_resume_check.py`): a worker
  is killed with SIGKILL while an entry load is mid-flight, restarted, and the ledger is
  then read back.

  | | |
  | --- | --- |
  | journal entries in the source system | 4,157 |
  | journal entries in the ledger afterwards | 4,157 |
  | duplicate `(source_system, external_ref)` pairs | 0 |
  | LOAD phase attempts recorded | 1 and 2 |
  | job status | `NEEDS_REVIEW` |

  The two LOAD attempts are Temporal rescheduling the interrupted chunk. The abandoned
  attempt is marked failed and the phase carries a `retried` badge, so the console shows
  the interruption rather than hiding it behind whichever row sorted last.
* **Reconciliation** on the seeded books, run as of the seeded cutover date: 15 breaks,
  every one of the 14 deliberately injected anomalies correctly attributed, plus one
  emergent timing break on the cash account that the late entries touch.

  | classification | count | what it means |
  | --- | --- | --- |
  | `ROUNDING` | 3 | difference below half a cent |
  | `FX` | 3 | residual on a foreign currency balance |
  | `TIMING` | 4 | activity dated after the cutover |
  | `MAPPING_ERROR` | 3 | an account on one side with no counterpart on the other |
  | `TRUE_BREAK` | 2 | unexplained, needs a human |

  4 of them are high severity and unresolved, which is what holds the job at
  `NEEDS_REVIEW`. Legacy debit 244,980,367.05 against loaded debit 245,686,620.51, a
  difference of 706,253.46 on a 245 million balance sheet.
* **Rate limiting is real**: the migration log shows
  `retryable status=429 attempt=1 sleeps=0.99s`, which is the client honouring the
  simulator's `Retry-After` rather than a claim about retry handling.
* **The variance query** runs in 12.6 ms on this dataset. The full, unabridged plan and an
  honest reading of why it is all sequential scans at this size: `docs/query-plans.md`.
* **The console** was driven in a headless Chrome against the live API by
  `scripts/console_check.cjs`, 14 checks, all passing: the dashboard renders real stats
  counted from the ledger tables; the new-migration form POSTs, defaults the cutover date
  from the API, and routes to the job page; the phase timeline lists every chunk and
  attempt with its own stats; the reconciliation table ranks breaks by size with a running
  share of the total; the inline resolve control posts and the row flips to Resolved with
  the note; polling stops on a terminal job; a disabled signal button carries a tooltip
  saying why; and a signal the API refuses (HTTP 200 with `ok:false`) is shown to the
  operator instead of being reported as accepted. `npm run build`, `npx tsc --noEmit` and
  `npm run lint` are clean.

  Setup for that script, since it is deliberately not a project dependency:

  ```bash
  mkdir -p /tmp/lb-pptr && cd /tmp/lb-pptr && npm init -y && npm i puppeteer-core
  node '<repo>/scripts/console_check.cjs'
  ```

## The demo that matters

See `docs/demo-runbook.md`. Short version:

1. Start a migration and watch the phase timeline advance.
2. `kill -9` the Temporal worker while the LOAD phase is running.
3. Restart the worker. Temporal reschedules the interrupted activity, and because
   every write is an idempotent upsert keyed on `(source_system, external_ref)`,
   the resumed load produces no duplicate rows.
4. The reconciliation report lists the breaks the seed deliberately injected,
   classified by cause.

Proof that step 3 is real, not asserted:

```bash
make crash-check   # kills the worker mid-load and reads the ledger back
make test          # includes tests/test_idempotency.py
```

`test_load_is_idempotent` runs the full load twice and asserts identical row
counts and identical account balances.

## Layout

```
backend/
  config/                 Django settings, URLs
  apps/ledger/            domain models, DRF API, load and reconcile services
  apps/migration/         Temporal workflows, activities, worker, inline runner
  apps/legacy_sim/        the fake legacy ERP: paginated, rate limited, flaky
  tests/                  double entry, idempotency, pagination, validation, reconciliation, API
frontend/                 Next.js operator console
scripts/                  crash_resume_check.py and console_check.cjs, the two commands
                          that prove the claims end to end
docs/                     design note, demo runbook, query plans
```

## Configuration

Copy `.env.example` to `.env`. Nothing needs changing for local development
except optionally `TEMPORAL_ENABLED`.

Set `TEMPORAL_ENABLED=false` to run migrations in a background thread instead of
through Temporal. Activities are plain functions, so the same code path runs
either way. Useful for tests and for a machine without the Temporal CLI. The
crash-resume demo requires `TEMPORAL_ENABLED=true`.
