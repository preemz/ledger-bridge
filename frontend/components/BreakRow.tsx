"use client";

import { useState } from "react";
import { resolveBreak } from "@/lib/api";
import type { Break, BreakClassification } from "@/lib/types";
import { BREAK_CLASSIFICATIONS } from "@/lib/types";
import { StatusPill } from "./StatusPill";
import { Money } from "./Money";

/** One break row. Unresolved rows carry the inline resolve control. */
export function BreakRow({
  row,
  onResolved,
}: {
  row: Break;
  onResolved: (updated: Break) => void;
}) {
  const [classification, setClassification] = useState<BreakClassification>("ROUNDING");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    setBusy(true);
    setError(null);
    const result = await resolveBreak(row.id, { classification, note });
    setBusy(false);
    if (!result.ok) {
      setError(result.error);
      return;
    }
    onResolved(result.data);
  }

  return (
    <tr>
      <td className="num mono">
        {row.variance_rank > 0 ? row.variance_rank : "-"}
      </td>
      <td className="mono">{row.account_code || "-"}</td>
      <td>
        {row.account_name || "-"}
        {row.note ? <div className="break-note">{row.note}</div> : null}
      </td>
      <td>
        <Money value={row.legacy_balance} />
      </td>
      <td>
        <Money value={row.loaded_balance} />
      </td>
      <td>
        <Money value={row.variance} signed />
      </td>
      <td className="num mono">
        {row.cumulative_variance_pct ? `${row.cumulative_variance_pct}%` : "-"}
      </td>
      <td>
        <StatusPill token={row.classification} />
      </td>
      <td>
        <StatusPill token={row.severity} />
      </td>
      <td>
        {row.resolved ? (
          <span className="resolved-line">Resolved</span>
        ) : (
          <div className="resolve-form">
            <select
              aria-label={`Classification for account ${row.account_code}`}
              value={classification}
              onChange={(e) =>
                setClassification(e.target.value as BreakClassification)
              }
              disabled={busy}
            >
              {BREAK_CLASSIFICATIONS.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
            <input
              type="text"
              aria-label={`Resolution note for account ${row.account_code}`}
              placeholder="Note (optional)"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              disabled={busy}
            />
            <button
              className="btn btn-sm"
              type="button"
              onClick={submit}
              disabled={busy}
            >
              {busy ? "Saving" : "Resolve"}
            </button>
            {error ? <span className="form-error">{error}</span> : null}
          </div>
        )}
      </td>
    </tr>
  );
}
