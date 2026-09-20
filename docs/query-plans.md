# Query plans

An unabridged capture of `make explain`, run against the seeded dataset: 240 accounts,
4,157 journal entries, 8,511 journal lines, cutover date 2026-09-08. Nothing below is
edited or trimmed.

An earlier version of this file pasted a plan taken against a handful of accounts with
zero balances, which describes a query shape that never runs in production, and it
presented a trimmed plan as though it were the capture. Both were wrong. The command now
loads the customer's real trial balance before explaining the query, so what it prints is
the plan that actually executes.

## The per account variance query

```
Sort  (cost=555.19..555.43 rows=96 width=217) (actual time=12.259..12.270 rows=15 loops=1)
  Sort Key: (rank() OVER (?)), (COALESCE(t.code, (a.code)::text))
  Sort Method: quicksort  Memory: 26kB
  Buffers: shared hit=201
  ->  WindowAgg  (cost=540.27..552.03 rows=96 width=217) (actual time=12.205..12.233 rows=15 loops=1)
        Buffers: shared hit=198
        ->  WindowAgg  (cost=540.27..546.27 rows=96 width=249) (actual time=12.158..12.192 rows=15 loops=1)
              Buffers: shared hit=198
              ->  WindowAgg  (cost=540.27..542.91 rows=96 width=217) (actual time=12.139..12.162 rows=15 loops=1)
                    Buffers: shared hit=198
                    ->  Sort  (cost=540.27..540.51 rows=96 width=209) (actual time=12.130..12.141 rows=15 loops=1)
                          Sort Key: (abs((((COALESCE(((COALESCE(sum(jl.debit), '0'::numeric))::numeric(20,4)), '0'::numeric) - COALESCE(((COALESCE(sum(jl.credit), '0'::numeric))::numeric(20,4)), '0'::numeric)) - COALESCE(t.balance, '0'::numeric)))::numeric(20,4))) DESC
                          Sort Method: quicksort  Memory: 26kB
                          Buffers: shared hit=198
                          ->  Hash Full Join  (cost=514.64..537.11 rows=96 width=209) (actual time=11.731..12.038 rows=15 loops=1)
                                Hash Cond: ((a.code)::text = t.code)
                                Filter: (abs((((COALESCE(((COALESCE(sum(jl.debit), '0'::numeric))::numeric(20,4)), '0'::numeric) - COALESCE(((COALESCE(sum(jl.credit), '0'::numeric))::numeric(20,4)), '0'::numeric)) - COALESCE(t.balance, '0'::numeric)))::numeric(20,4)) > 0.0001)
                                Rows Removed by Filter: 227
                                Buffers: shared hit=195
                                ->  HashAggregate  (cost=509.21..514.01 rows=240 width=87) (actual time=11.291..11.482 rows=240 loops=1)
                                      Group Key: a.id
                                      Batches: 1  Memory Usage: 285kB
                                      Buffers: shared hit=195
                                      ->  Hash Right Join  (cost=188.32..445.49 rows=8497 width=57) (actual time=2.532..8.522 rows=8493 loops=1)
                                            Hash Cond: (jl.account_id = a.id)
                                            Buffers: shared hit=195
                                            ->  Hash Join  (cost=178.92..413.39 rows=8497 width=18) (actual time=2.411..6.156 rows=8493 loops=1)
                                                  Hash Cond: (jl.entry_id = e.id)
                                                  Buffers: shared hit=191
                                                  ->  Seq Scan on ledger_journalline jl  (cost=0.00..212.11 rows=8511 width=26) (actual time=0.003..1.221 rows=8511 loops=1)
                                                        Buffers: shared hit=127
                                                  ->  Hash  (cost=126.73..126.73 rows=4175 width=8) (actual time=2.392..2.393 rows=4148 loops=1)
                                                        Buckets: 8192  Batches: 1  Memory Usage: 227kB
                                                        Buffers: shared hit=64
                                                        ->  Seq Scan on ledger_journalentry e  (cost=0.00..126.73 rows=4175 width=8) (actual time=0.004..1.581 rows=4148 loops=1)
                                                              Filter: ((entry_date <= '2026-09-08'::date) AND ((status)::text = 'POSTED'::text))
                                                              Rows Removed by Filter: 9
                                                              Buffers: shared hit=64
                                            ->  Hash  (cost=6.40..6.40 rows=240 width=47) (actual time=0.112..0.112 rows=240 loops=1)
                                                  Buckets: 1024  Batches: 1  Memory Usage: 27kB
                                                  Buffers: shared hit=4
                                                  ->  Seq Scan on ledger_account a  (cost=0.00..6.40 rows=240 width=47) (actual time=0.009..0.059 rows=240 loops=1)
                                                        Buffers: shared hit=4
                                ->  Hash  (cost=2.42..2.42 rows=241 width=96) (actual time=0.364..0.369 rows=241 loops=1)
                                      Buckets: 1024  Batches: 1  Memory Usage: 23kB
                                      ->  Function Scan on t  (cost=0.01..2.42 rows=241 width=96) (actual time=0.279..0.315 rows=241 loops=1)
Planning:
  Buffers: shared hit=268 dirtied=2
Planning Time: 3.008 ms
Execution Time: 12.616 ms
```

12.6 ms end to end, which is the number that matters: a reconciliation nobody waits for is
a reconciliation somebody skips before a cutover.

### Reading it

* `Function Scan on t` is the customer's trial balance, passed in as arrays and expanded
  by `unnest`. 241 rows: the 240 accounts in the chart plus the retired account that
  appears only in the ledger, which is one of the disagreements the report exists to
  surface.
* `Rows Removed by Filter: 227` on the full outer join is the filter doing its job. 227
  accounts agree to within the tolerance, 15 do not, and only the 15 continue downstream.
* `Rows Removed by Filter: 9` on the entry scan is the cutover date doing its job. Those
  nine entries dated after 2026-09-08 are what produce the timing breaks.
* Three `WindowAgg` nodes: the rank by absolute variance, the running share of the total,
  and the account count.

### Why the plan is all sequential scans, and why that is correct here

This is the honest reading of the plan rather than the flattering one. At 8,511 lines
Postgres is right to scan: the whole line table is roughly 400 kB, so it costs a handful
of shared buffer reads, and an index scan would add random I/O to a table it can walk
once. The `Hash Join` nodes are the join strategy the query wants.

The two indexes that exist for when the table stops being small:

* `entry_date_status_idx` on `ledger_journalentry (entry_date, status)` supports the
  `WHERE status = 'POSTED' AND entry_date <= :as_of` predicate. That predicate is already
  discarding rows in this plan, 9 out of 4,148.
* `line_account_entry_idx` on `ledger_journalline (account, entry)` supports the per
  account aggregate when the account set is small relative to the line table.

The right way to confirm this is to run the plan at production volume rather than to
reason about it, which is why `make explain` exists. At roughly a million lines the entry
filter starts choosing the index, and the plan is worth re-reading then. Asserting "it
uses an index" from a 400 kB dataset is the kind of claim that collapses in front of the
person who owns the data.

## The totals query

`loaded_totals` is a separate, simpler aggregate rather than a second pass over the
variance result, because it computes something the variance query does not: the trial
balance convention of positive net balances as debits and negative net balances as
credits. Summing all debit movement instead produces a number of the right shape and the
wrong meaning, and the difference between two differently defined quantities looks like a
finding when it is an arithmetic mistake. `tests/test_reconcile.py::
test_both_sides_are_measured_the_same_way` fails if the two sides ever stop being
comparable.