# Design note: migrating a finance team off a legacy ERP

This is the write-up for the "customer facing project you personally owned from
discovery to production" question. It is written the way the engagement actually runs,
in the order it runs.

## Discovery

Six questions decide the shape of the whole project, and none of them are technical:

1. **What is the cutover date, exactly?** Every accounting migration has one, and the
   reconciliation is meaningless without it. Activity after the cutover belongs to the
   customer's old process, not to the new ledger. Getting this wrong produces hundreds
   of false breaks, which destroys trust in the report before it is read.
2. **What is the source of truth for the trial balance?** Some finance teams reconcile
   to the general ledger, some to a management report that has already been adjusted.
   If you do not ask, you will reconcile against the wrong side.
3. **Who signs off, and what do they need to see?** Usually a finance lead who wants a
   per-account variance report and an explanation for each line, not a row count.
4. **How does the legacy system hand over data?** API, database replica, flat files. In
   practice it is "all three, depending on the table", and the API is rate limited.
5. **What happens on failure?** The customer needs to know the ledger is either fully
   loaded or not loaded at all. Partial states are the thing that terrifies them.
6. **What does the audit requirement look like?** In regulated finance it is usually
   "explain any number at any point in the past", which forces an append-only log from
   day one rather than a retrofitted one.

## Scope and design decisions

**Extract, stage, then transform. Do not stream straight into the ledger.** The legacy
system is unreliable and rate limited. Reading it once into a durable staging table
means every later failure is re-processed locally instead of against the customer's
production ERP. It also gives a resume point that a straight pipeline cannot have.

**Idempotency is a data model decision, not a retry policy.** Every row carries the
source system's identifier, and the ledger has a unique constraint on
`(source_system, external_ref)`. Loads upsert on that key. This is what makes
at-least-once execution safe, and at-least-once is all any workflow engine gives you.

Upsert rather than delete-and-reinsert: delete-and-reinsert is not idempotent in the
way that matters, because a crash between the delete and the insert leaves the customer
short of entries. The failure mode of an upsert is one stale row. The failure mode of
delete-and-reinsert is four thousand missing ones.

**Durability per chunk, not per phase.** A four minute phase that dies loses four
minutes. The same phase split into twenty chunks loses one. Long phases are loops of
chunk activities, each with its own checkpoint row and its own idempotency key.

**The database enforces double entry.** A deferred constraint trigger checks
`sum(debits) = sum(credits)` per entry at COMMIT. Application validation is a
convention that holds until somebody writes to the table from a shell, a migration or
a support script. The trigger holds either way.

**The audit log is append only, enforced.** A finance team has to be able to explain
any number months later. The application must not be able to edit the record of what
it did, so the database refuses UPDATE and DELETE.

**Reconciliation names the cause.** A report that says "47 accounts differ" is a report
that gets escalated to you. A report that says "31 are rounding artifacts below half a
cent, 9 are post cutover activity, 5 are accounts missing from your chart of accounts,
2 are unexplained and need you" is a report that gets worked. Classification runs in a
fixed order, most benign explanation first, and every break carries the reasoning that
produced it.

**A completed migration is not a signed off one.** The job ends `NEEDS_REVIEW` when
anything was rejected in validation or an unexplained high severity break remains. The
migration does not get to declare victory on the customer's behalf.

**A retry of the reconciliation replaces its previous result.** One job and one cutover
date is one comparison. Appending would leave several runs, each holding a full copy of
every break, and the console would show whichever one the ordering happened to surface.
The same reasoning applies to every phase: each chunk is a checkpoint keyed on the job,
the phase, the attempt and the chunk, and an attempt that a killed worker abandoned is
marked failed so the timeline reports an interruption rather than work in progress.

**The audit log records failures, not activity.** Phase successes are already in the
phase timeline with their per chunk stats. Mirroring every successful chunk into the
audit log produced about twenty rows of work that went right for a 4,000 entry
migration, which buries the rows that describe work that went wrong. What a finance
team needs to find months later is the failure, the reconciliation result, the operator
decisions and the status changes, so those are what the log holds.

## Production rollout

1. **Dry run against a copy.** Same code path, the customer's real data, a scratch
   database. The reconciliation report from the dry run is the artefact that gets
   reviewed, weeks before cutover.
2. **Freeze, then migrate, then verify, in one window.** The customer stops posting in
   the old system. The migration runs against a frozen source, so the reconciliation is
   a fixed target rather than a moving one.
3. **Verify before switching users on.** The finance lead reads the variance report and
   resolves or accepts each break. Any unresolved true break is a blocker, not a note.
4. **Keep the old system read only for a quarter.** Disputes surface late, and being
   able to re-run the reconciliation against the old system is what settles them.
5. **Turn the one-off into a product surface.** Every migration needs a cutover date, a
   chart of accounts mapping, a variance report and a resume point. Those are the
   pieces other customers will need, and they belong in the platform rather than in
   another script.

## What I would do differently

**Model the chart of accounts mapping as data from the start.** It is currently type
aliases in one Python module, which is right for the first customer and wrong for the
fourth. Mapping belongs in a table with a review step, because the customer's finance
team is the authority on what their account codes mean, not the integration engineer.

**Reconcile during the load, not only at the end.** A running variance check would let
the report flag a drift while the load is still going, which shaves the feedback loop
that the role is explicitly asking to shorten.

## Trade-offs taken deliberately

* **PostgreSQL over an accounting ledger package.** The role is explicit that this is
  advanced Postgres work, and every accountant-shaped abstraction I could have added
  would have hidden exactly the query behaviour the job is about.
* **Temporal over Celery.** The role states a preference. The deciding factor is that
  the crash-resume property is visible in the workflow event history, which is what
  makes it reviewable by somebody who was not there when it was built.
* **Simulated legacy ERP rather than a real connector.** The interesting engineering is
  in the reconciliation, not in an OAuth dance against a vendor's sandbox. The
  simulator deliberately misbehaves: it paginates, rate limits, fails intermittently,
  and disagrees with its own journal.
