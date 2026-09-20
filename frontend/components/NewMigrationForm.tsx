"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { createJob } from "@/lib/api";
import { SOURCE_SYSTEMS } from "@/lib/types";
import type { SourceSystem } from "@/lib/types";

function today(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/**
 * Creates a migration job, then routes to the new job detail page.
 *
 * The as-of date defaults to the cutover date of the seeded dataset rather than to today.
 * That default is the whole reason the demo has anything interesting to show: the seeded
 * books contain activity dated after the cutover, and running as of today pulls that
 * activity in, so the timing breaks disappear. The value arrives from the stats endpoint
 * instead of being hardcoded, and the date field starts empty on the server so the
 * rendered markup does not depend on the clock.
 */
export function NewMigrationForm({ datasetAsOfDate }: { datasetAsOfDate: string | null }) {
  const router = useRouter();
  const [customerReference, setCustomerReference] = useState("");
  const [sourceSystem, setSourceSystem] = useState<SourceSystem>("legacy_erp");
  const [asOfDate, setAsOfDate] = useState(datasetAsOfDate ?? "");
  const [dateTouched, setDateTouched] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (dateTouched) return;
    if (datasetAsOfDate) {
      setAsOfDate(datasetAsOfDate);
    } else if (asOfDate === "") {
      setAsOfDate(today());
    }
  }, [datasetAsOfDate, dateTouched, asOfDate]);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    if (customerReference.trim() === "") {
      setError("Customer reference is required.");
      return;
    }
    if (asOfDate === "") {
      setError("As-of date is required.");
      return;
    }
    setSubmitting(true);
    const result = await createJob({
      customer_reference: customerReference.trim(),
      source_system: sourceSystem,
      as_of_date: asOfDate,
    });
    setSubmitting(false);
    if (!result.ok) {
      setError(result.error);
      return;
    }
    setCustomerReference("");
    router.push(`/jobs/${result.data.id}`);
  }

  return (
    <form className="form-row" onSubmit={handleSubmit}>
      <div className="field">
        <label className="field-label" htmlFor="customer_reference">
          Customer reference
        </label>
        <input
          id="customer_reference"
          type="text"
          name="customer_reference"
          placeholder="e.g. NORTHWIND-2026Q1"
          value={customerReference}
          onChange={(e) => setCustomerReference(e.target.value)}
          autoComplete="off"
        />
      </div>
      <div className="field">
        <label className="field-label" htmlFor="source_system">
          Source system
        </label>
        <select
          id="source_system"
          name="source_system"
          value={sourceSystem}
          onChange={(e) => setSourceSystem(e.target.value as SourceSystem)}
        >
          {SOURCE_SYSTEMS.map((system) => (
            <option key={system} value={system}>
              {system}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label className="field-label" htmlFor="as_of_date">
          As-of date
        </label>
        <input
          id="as_of_date"
          type="date"
          name="as_of_date"
          value={asOfDate}
          onChange={(e) => {
            setDateTouched(true);
            setAsOfDate(e.target.value);
          }}
        />
        <span className="form-hint" id="as_of_date_hint" style={{ display: "block", marginTop: 4 }}>
          {datasetAsOfDate
            ? "Defaults to the cutover date of the seeded dataset. Activity after it is excluded from the reconciliation, which is what produces the timing breaks."
            : "No seeded dataset found, so this defaults to today."}
        </span>
      </div>
      <button className="btn btn-primary" type="submit" disabled={submitting}>
        {submitting ? "Starting" : "New migration"}
      </button>
      {error ? <span className="form-error">{error}</span> : null}
    </form>
  );
}
