# Demo runbook

Total run time about fifteen minutes. The only part that needs rehearsal is the kill in
step 4, so practise that once before recording.

## Setup, once

```bash
brew install postgresql@16 temporal
brew services start postgresql@16
createdb ledger_bridge

cd "Personal projects/ledger-bridge"
make install
make migrate
make seed
```

`make seed` prints the cutover date it generated. The console defaults the migration
form to that date, which is deliberate: the seeded books contain activity after it, and
running as of that date is what produces a reconciliation report with something in it.

Three terminals:

```bash
temporal server start-dev     # or: make temporal
make api
make worker
```

Optional fourth, for the console: `make web` then open http://localhost:3000.

To make the kill window comfortable, slow the load deliberately:

```bash
MIGRATION_PACING_MS=400 make worker
```

That sleeps 400ms between load batches. It has no effect on anything except the demo,
and the default is zero.

## The run

**1. Show the source system.**
```bash
curl -s localhost:8000/legacy-api/v1/trial-balance \
  -H "Authorization: Bearer legacy-dev-token" | head -c 400
```
Point out that this is the customer's system: it paginates, it rate limits, and it
fails. Try it twice quickly and you can see a 429.

**2. Start a migration.**
```bash
curl -s -X POST localhost:8000/api/jobs/ \
  -H 'Content-Type: application/json' \
  -d '{"customer_reference":"Northwind Trading","source_system":"legacy_erp"}'
```

In the console you get the phase timeline filling in: EXTRACT, STAGE, TRANSFORM,
VALIDATE, LOAD, RECONCILE. Each phase row is a durable checkpoint.

**3. Point at the audit log.** Every status change is a row, and the table refuses
UPDATE and DELETE at the database level:
```bash
psql ledger_bridge -c "update ledger_auditevent set message = 'edited' where id = 1;"
# ERROR: ledger_auditevent is append-only
```

**4. Kill the worker mid-load. This is the moment.**
In the LOAD phase:
```bash
pkill -9 -f "manage.py run_worker"
```
The phase shows as failed, Temporal notices the missed heartbeat. Restart:
```bash
make worker
```
Temporal reschedules the interrupted chunk, the activity replays, and because every
write is an upsert keyed on `(source_system, external_ref)` nothing is duplicated.

**5. Prove there was no duplication.**

One command does the whole thing, including the kill, and reads the database back:

```bash
make crash-check
```

It exits non-zero if the worker was never interrupted, if any journal entry was written
twice, or if the ledger count does not match the source count. To do it by hand instead:

```bash
psql ledger_bridge -c "
select (select count(*) from ledger_journalentry) as entries,
       (select count(*) from ledger_journalentry e
         where (select count(*) from ledger_journalentry x
                 where x.external_ref = e.external_ref) > 1) as duplicated_refs;"
```
`duplicated_refs` is zero. Then:
```bash
make test        # includes test_load_is_idempotent and a_replayed_load_chunk_writes_nothing_new
```

**6. Read the reconciliation out loud.** The report lists the accounts where the
migrated ledger and the customer's trial balance disagree, ranked by size so the largest
difference is the first row, with a running share of the total so you can see how much of
the problem the first few rows account for. Each break carries a cause:

| Cause | What it means |
| --- | --- |
| ROUNDING | The ERP exports two decimal places, so a sub-cent difference appears |
| FX | A foreign currency balance carries a translation residual |
| TIMING | Activity dated after the cutover, so an as-of comparison cannot see it |
| MAPPING_ERROR | An account on one side has no counterpart on the other |
| TRUE_BREAK | No benign explanation. Somebody has to look. |

The seeded dataset contains deliberately one of each. The test suite asserts the
classifier lands on the right cause for the right account, not merely that breaks exist.

Close on the job status: the run finished, and it is `NEEDS_REVIEW` rather than
`COMPLETED`, because an unexplained high severity break holds the job until a human
resolves it. That is the honesty the role is asking for.

## What to say about the design, in one breath

The extract is staged before it is loaded, so a flaky third party is read once. Every
phase is chunked, so a crash costs a chunk rather than a phase. Every write is an
upsert, so a retry is safe. The audit log is append only, so the run is explainable
months later. And the reconciliation refuses to call a migration done while an
unexplained difference remains, because the customer's finance team signs off, not the
migration script.
