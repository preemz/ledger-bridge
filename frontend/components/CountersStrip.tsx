import type { Counters } from "@/lib/types";
import { COUNTER_LABELS } from "@/lib/types";
import { formatInteger } from "@/lib/format";

/** Extra counter keys the API sends beyond the documented set. */
function extraCounters(counters: Counters): Array<[string, number]> {
  const record: Record<string, unknown> = { ...counters };
  return Object.entries(record)
    .filter(([key]) => !COUNTER_LABELS.some(([known]) => known === key))
    .map(([key, value]) => [key, typeof value === "number" ? value : 0]);
}

/** Renders every declared counter. Keys missing from the API show as 0. */
export function CountersStrip({ counters }: { counters: Counters }) {
  return (
    <div className="counter-strip">
      {COUNTER_LABELS.map(([key, label]) => (
        <div className="counter-cell" key={key}>
          <div className="counter-label">{label}</div>
          <div className="counter-value">{formatInteger(counters[key] ?? 0)}</div>
        </div>
      ))}
      {extraCounters(counters).map(([key, value]) => (
        <div className="counter-cell" key={key}>
          <div className="counter-label">{key}</div>
          <div className="counter-value">{formatInteger(value)}</div>
        </div>
      ))}
    </div>
  );
}
