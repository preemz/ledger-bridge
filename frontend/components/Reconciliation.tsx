import type { Break, ReconciliationRun } from "@/lib/types";
import { formatInteger, formatTimestamp, humanizeToken } from "@/lib/format";
import { Money } from "./Money";
import { BreakRow } from "./BreakRow";
import { EmptyState } from "./Notice";

export function ReconciliationSummary({ run }: { run: ReconciliationRun }) {
  return (
    <div className="run-summary">
      <div className="run-cell">
        <div className="run-label">As of</div>
        <div className="run-value">{run.as_of_date || "-"}</div>
      </div>
      <div className="run-cell run-cell-total">
        <div className="run-label">Legacy debit</div>
        <Money className="run-value" value={run.legacy_total_debit} />
      </div>
      <div className="run-cell">
        <div className="run-label">Legacy credit</div>
        <Money className="run-value" value={run.legacy_total_credit} />
      </div>
      <div className="run-cell run-cell-total">
        <div className="run-label">Loaded debit</div>
        <Money className="run-value" value={run.loaded_total_debit} />
      </div>
      <div className="run-cell">
        <div className="run-label">Loaded credit</div>
        <Money className="run-value" value={run.loaded_total_credit} />
      </div>
      <div className="run-cell">
        <div className="run-label">Legacy entries</div>
        <div className="run-value">{formatInteger(run.legacy_entry_count)}</div>
      </div>
      <div className="run-cell">
        <div className="run-label">Loaded entries</div>
        <div className="run-value">{formatInteger(run.loaded_entry_count)}</div>
      </div>
      <div className="run-cell">
        <div className="run-label">Difference</div>
        <Money className="run-value" value={run.difference} signed />
      </div>
      <div className="run-cell">
        <div className="run-label">Run status</div>
        <div className="run-value">{humanizeToken(run.status) || "-"}</div>
      </div>
      <div className="run-cell">
        <div className="run-label">Created</div>
        <div className="run-value">{formatTimestamp(run.created_at)}</div>
      </div>
    </div>
  );
}

/**
 * Break table. Unresolved first, then by variance rank.
 *
 * The order is the rank the server computed, so the list reads exactly as "largest
 * difference first" and the cumulative column tells the reviewer how much of the total
 * the rows above account for. Sorting by severity instead buried the largest differences
 * below smaller ones that happened to be high severity.
 */
export function ReconciliationBreaks({
  breaks,
  onResolved,
}: {
  breaks: Break[];
  onResolved: (updated: Break) => void;
}) {
  if (breaks.length === 0) {
    return <EmptyState>No reconciliation breaks recorded for this run.</EmptyState>;
  }
  const ordered = [...breaks].sort((a, b) => {
    if (a.resolved !== b.resolved) return a.resolved ? 1 : -1;
    return (a.variance_rank || 0) - (b.variance_rank || 0);
  });
  const unresolved = ordered.filter((row) => !row.resolved).length;

  return (
    <div className="table-wrap">
      <table className="data">
        <thead>
          <tr>
            <th className="num">Rank</th>
            <th>Account code</th>
            <th>Account name</th>
            <th className="num">Legacy balance</th>
            <th className="num">Loaded balance</th>
            <th className="num">Variance</th>
            <th className="num" title="Share of the total difference, this break and all larger ones">
              Cumulative
            </th>
            <th>Classification</th>
            <th>Severity</th>
            <th>
              {unresolved > 0 ? `Unresolved (${unresolved})` : "Unresolved"}
            </th>
          </tr>
        </thead>
        <tbody>
          {ordered.map((row) => (
            <BreakRow key={row.id} row={row} onResolved={onResolved} />
          ))}
        </tbody>
      </table>
    </div>
  );
}
