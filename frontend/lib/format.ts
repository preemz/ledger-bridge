// Shared formatting helpers. All money formatting lives here so the whole
// console renders decimal strings the same way.

const moneyFormatter = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const integerFormatter = new Intl.NumberFormat("en-US", {
  maximumFractionDigits: 0,
});

/** Parse a decimal string such as "12345.6789" or a number into a number. */
export function parseDecimal(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

/** Format a decimal string to 2 decimals with thousands separators. */
export function formatMoney(value: string | number | null | undefined): string {
  const n = parseDecimal(value);
  if (n === null) return "-";
  return moneyFormatter.format(n);
}

/** Format a decimal string with an explicit sign, useful for variance columns. */
export function formatSignedMoney(value: string | number | null | undefined): string {
  const n = parseDecimal(value);
  if (n === null) return "-";
  if (n > 0) return "+" + moneyFormatter.format(n);
  return moneyFormatter.format(n);
}

/** True when the value parses to a negative number. */
export function isNegative(value: string | number | null | undefined): boolean {
  const n = parseDecimal(value);
  return n !== null && n < 0;
}

export function formatInteger(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "0";
  return integerFormatter.format(value);
}

export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "-";
  return integerFormatter.format(value);
}

/** Timestamp for tables and logs. Returns "-" for missing or unparsable input. */
export function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "-";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "-";
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  );
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "-";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** Duration between two timestamps. Non-terminal phases show elapsed time. */
export function formatDuration(
  startedAt: string | null | undefined,
  finishedAt: string | null | undefined,
): string {
  if (!startedAt) return "-";
  const start = new Date(startedAt).getTime();
  if (Number.isNaN(start)) return "-";
  const end = finishedAt ? new Date(finishedAt).getTime() : Date.now();
  if (Number.isNaN(end)) return "-";
  const seconds = Math.max(0, Math.round((end - start) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  if (minutes < 60) return `${minutes}m ${rest}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

/**
 * A duration that is already measured, in milliseconds.
 *
 * The per chunk `duration_ms` the API records is not derivable from the timestamps at
 * display time: a finished chunk's timestamps are both present, so subtracting them would
 * work, but a chunk still running has no end, and the API number is the one the activity
 * actually measured.
 */
export function formatDurationMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms) || ms < 0) return "-";
  const seconds = ms / 1000;
  if (seconds < 10) return `${seconds.toFixed(1)}s`;
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  if (minutes < 60) return `${minutes}m ${rest}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

/** Human label for an enum token, e.g. MAPPING_ERROR -> Mapping error. */
export function humanizeToken(token: string): string {
  const cleaned = token.replace(/_/g, " ").toLowerCase();
  if (!cleaned) return "";
  return cleaned.charAt(0).toUpperCase() + cleaned.slice(1);
}
