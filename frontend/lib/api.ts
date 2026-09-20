// Single place where the console talks to the Ledger Bridge API.
//
// Every function returns an ApiResult and never throws: the UI must stay
// mounted and readable while the backend is down or erroring.

import type {
  AuditEvent,
  Break,
  BreakClassification,
  BreakSeverity,
  Counters,
  Job,
  JobStatus,
  JobWithDetail,
  NewJobInput,
  Phase,
  PhaseStatus,
  ReconciliationRun,
  ResolveInput,
  Signal,
  SignalResponse,
  Stats,
} from "./types";

export const API_BASE: string = (
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000"
).replace(/\/+$/, "");

export type ApiResult<T> =
  | { ok: true; data: T }
  | { ok: false; error: string };

const NOT_REACHABLE = "Backend unreachable - retrying";

/* ------------------------------------------------------------------ */
/* Narrowing helpers. Unknown JSON in, typed values out, no `any`.     */
/* ------------------------------------------------------------------ */

function asRecord(value: unknown): Record<string, unknown> {
  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  return {};
}

function asString(value: unknown, fallback = ""): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return fallback;
}

function asNullableString(value: unknown): string | null {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return null;
  if (typeof value === "number") return String(value);
  return null;
}

function asNumber(value: unknown, fallback = 0): number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const n = Number(value);
    if (Number.isFinite(n)) return n;
  }
  return fallback;
}

function asBoolean(value: unknown): boolean {
  if (typeof value === "boolean") return value;
  if (typeof value === "string") return value === "true";
  return false;
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

/** Decimal money fields stay as strings end to end. */
function asDecimalString(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return "0";
}

const JOB_STATUSES: readonly JobStatus[] = [
  "PENDING",
  "RUNNING",
  "PAUSED",
  "COMPLETED",
  "FAILED",
  "NEEDS_REVIEW",
];

const PHASE_STATUSES: readonly PhaseStatus[] = [
  "PENDING",
  "RUNNING",
  "COMPLETED",
  "FAILED",
  "SKIPPED",
];

const CLASSIFICATIONS: readonly BreakClassification[] = [
  "UNCLASSIFIED",
  "ROUNDING",
  "TIMING",
  "FX",
  "MAPPING_ERROR",
  "TRUE_BREAK",
];

function parseItem(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/**
 * Phase stats for display. Values arrive as whatever the activity returned, which
 * includes nested objects such as the retry statistics, so they are stringified here
 * rather than dropped: the retry counts are some of the most interesting numbers on the
 * page, and an earlier version of this silently filtered them out.
 */
function asDisplayMap(value: unknown): Record<string, string> {
  const record = asRecord(value);
  const out: Record<string, string> = {};
  for (const [key, raw] of Object.entries(record)) {
    const text = parseItem(raw);
    if (text) out[key] = text.length > 120 ? `${text.slice(0, 117)}...` : text;
  }
  return out;
}

function asJobStatus(value: unknown): JobStatus {
  const raw = asString(value).toUpperCase();
  const match = JOB_STATUSES.find((s) => s === raw);
  return match ?? "PENDING";
}

function asSignal(value: unknown, fallback: Signal): Signal {
  const raw = asString(value).toLowerCase();
  return raw === "pause" || raw === "resume" || raw === "cancel" ? raw : fallback;
}

function asPhaseStatus(value: unknown): PhaseStatus {
  const raw = asString(value).toUpperCase();
  const match = PHASE_STATUSES.find((s) => s === raw);
  return match ?? "PENDING";
}

function asSeverity(value: unknown): BreakSeverity {
  const raw = asString(value).toUpperCase();
  if (raw === "HIGH" || raw === "MEDIUM" || raw === "LOW") return raw;
  return "LOW";
}

function asClassification(value: unknown): BreakClassification {
  const raw = asString(value).toUpperCase();
  const match = CLASSIFICATIONS.find((s) => s === raw);
  return match ?? "UNCLASSIFIED";
}

/* ------------------------------------------------------------------ */
/* Response parsers                                                    */
/* ------------------------------------------------------------------ */

function parseCounters(value: unknown): Counters {
  const record = asRecord(value);
  const counters: Counters = {};
  const keys: Array<keyof Counters> = [
    "rows_read",
    "rows_staged",
    "rows_loaded",
    "rows_skipped",
    "entries_loaded",
    "lines_loaded",
    "breaks_found",
  ];
  for (const key of keys) {
    const raw = record[key];
    if (typeof raw === "number" && Number.isFinite(raw)) {
      counters[key] = raw;
    } else if (typeof raw === "string" && raw.trim() !== "") {
      const n = Number(raw);
      if (Number.isFinite(n)) counters[key] = n;
    }
  }
  return counters;
}

export function parseJob(value: unknown): Job {
  const record = asRecord(value);
  return {
    id: asString(record.id),
    customer_reference: asString(record.customer_reference),
    source_system: asString(record.source_system),
    status: asJobStatus(record.status),
    temporal_workflow_id: asNullableString(record.temporal_workflow_id),
    counters: parseCounters(record.counters),
    started_at: asNullableString(record.started_at),
    finished_at: asNullableString(record.finished_at),
    created_at: asString(record.created_at),
    error: asNullableString(record.error),
  };
}

export function parsePhase(value: unknown): Phase {
  const record = asRecord(value);
  const durationRaw = record.duration_ms;
  const duration = typeof durationRaw === "number" ? durationRaw : null;
  return {
    id: asString(record.id),
    name: asString(record.name),
    status: asPhaseStatus(record.status),
    attempt: asNumber(record.attempt, 0),
    chunk: asNumber(record.chunk, 0),
    label: asString(record.label),
    started_at: asNullableString(record.started_at),
    finished_at: asNullableString(record.finished_at),
    duration_ms: duration,
    stats: asDisplayMap(record.stats),
    error: asNullableString(record.error),
  };
}

export function parseRun(value: unknown): ReconciliationRun {
  const record = asRecord(value);
  return {
    id: asString(record.id),
    job: asString(record.job),
    as_of_date: asString(record.as_of_date),
    status: asString(record.status),
    legacy_total_debit: asDecimalString(record.legacy_total_debit),
    legacy_total_credit: asDecimalString(record.legacy_total_credit),
    loaded_total_debit: asDecimalString(record.loaded_total_debit),
    loaded_total_credit: asDecimalString(record.loaded_total_credit),
    legacy_entry_count: asNumber(record.legacy_entry_count, 0),
    loaded_entry_count: asNumber(record.loaded_entry_count, 0),
    difference: asDecimalString(record.difference),
    created_at: asString(record.created_at),
  };
}

export function parseBreak(value: unknown): Break {
  const record = asRecord(value);
  return {
    id: asString(record.id),
    run: asString(record.run),
    account_code: asString(record.account_code),
    account_name: asString(record.account_name),
    legacy_balance: asDecimalString(record.legacy_balance),
    loaded_balance: asDecimalString(record.loaded_balance),
    variance: asDecimalString(record.variance),
    variance_rank: asNumber(record.variance_rank, 0),
    cumulative_variance_pct: asDecimalString(record.cumulative_variance_pct),
    classification: asClassification(record.classification),
    severity: asSeverity(record.severity),
    resolved: asBoolean(record.resolved),
    note: asString(record.note),
  };
}

export function parseStats(value: unknown): Stats {
  const record = asRecord(value);
  return {
    jobs_total: asNumber(record.jobs_total, 0),
    jobs_running: asNumber(record.jobs_running, 0),
    jobs_completed: asNumber(record.jobs_completed, 0),
    breaks_open: asNumber(record.breaks_open, 0),
    entries_loaded: asNumber(record.entries_loaded, 0),
    lines_loaded: asNumber(record.lines_loaded, 0),
    accounts_loaded: asNumber(record.accounts_loaded, 0),
    dataset_as_of_date: asNullableString(record.dataset_as_of_date),
  };
}

export function parseAuditEvent(value: unknown): AuditEvent {
  const record = asRecord(value);
  return {
    id: asNumber(record.id, 0),
    event_type: asString(record.event_type),
    actor: asString(record.actor),
    message: asString(record.message),
    payload: record.payload ?? null,
    created_at: asString(record.created_at),
  };
}

export function parseJobWithDetail(value: unknown): JobWithDetail {
  const base = parseJob(value);
  const record = asRecord(value);
  const reconciliationRaw = record.reconciliation;
  let reconciliation: JobWithDetail["reconciliation"] = null;
  if (reconciliationRaw !== null && reconciliationRaw !== undefined) {
    const recon = asRecord(reconciliationRaw);
    reconciliation = {
      run: parseRun(recon.run),
      breaks: asArray(recon.breaks).map(parseBreak),
    };
  }
  return {
    ...base,
    phases: asArray(record.phases).map(parsePhase),
    reconciliation,
  };
}

/* ------------------------------------------------------------------ */
/* Transport                                                           */
/* ------------------------------------------------------------------ */

interface RequestOptions {
  method?: "GET" | "POST";
  body?: unknown;
  /** Abort the request after this many ms. */
  timeoutMs?: number;
}

async function request<T>(
  path: string,
  parse: (json: unknown) => T,
  options: RequestOptions = {},
): Promise<ApiResult<T>> {
  const { method = "GET", body, timeoutMs = 8000 } = options;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
      cache: "no-store",
    });
    if (!response.ok) {
      return { ok: false, error: `API error ${response.status} on ${path}` };
    }
    if (response.status === 204) {
      return { ok: true, data: parse(null) };
    }
    const json: unknown = await response.json();
    return { ok: true, data: parse(json) };
  } catch {
    return { ok: false, error: NOT_REACHABLE };
  } finally {
    clearTimeout(timer);
  }
}

/* ------------------------------------------------------------------ */
/* Endpoints                                                           */
/* ------------------------------------------------------------------ */

export function getStats(): Promise<ApiResult<Stats>> {
  return request("/api/stats/", parseStats);
}

export function getJobs(): Promise<ApiResult<Job[]>> {
  return request("/api/jobs/", (json) => asArray(json).map(parseJob));
}

export function getJob(id: string): Promise<ApiResult<JobWithDetail>> {
  return request(`/api/jobs/${encodeURIComponent(id)}/`, parseJobWithDetail);
}

export function getAudit(id: string): Promise<ApiResult<AuditEvent[]>> {
  return request(`/api/jobs/${encodeURIComponent(id)}/audit/`, (json) =>
    asArray(json).map(parseAuditEvent),
  );
}

export function createJob(input: NewJobInput): Promise<ApiResult<Job>> {
  return request("/api/jobs/", parseJob, { method: "POST", body: input, timeoutMs: 15000 });
}

export function sendSignal(id: string, signal: Signal): Promise<ApiResult<SignalResponse>> {
  return request(
    `/api/jobs/${encodeURIComponent(id)}/signal/`,
    (json) => {
      const record = asRecord(json);
      return {
        // The payload's own flag, not the transport's. See SignalResponse.
        ok: asBoolean(record.ok),
        signal: asSignal(record.signal, signal),
        status: asJobStatus(record.status),
        detail: asString(record.detail),
      };
    },
    { method: "POST", body: { signal }, timeoutMs: 15000 },
  );
}

export function resolveBreak(
  id: string,
  input: ResolveInput,
): Promise<ApiResult<Break>> {
  return request(`/api/breaks/${encodeURIComponent(id)}/resolve/`, parseBreak, {
    method: "POST",
    body: input,
    timeoutMs: 15000,
  });
}

export function isTerminalStatus(status: JobStatus): boolean {
  // NEEDS_REVIEW is terminal. The backend's MigrationJob status list treats it that way,
  // and a job held for review will not change again: polling it forever re-fetches the
  // same payload every two seconds and never shows the "polling stopped" notice.
  return status === "COMPLETED" || status === "FAILED" || status === "NEEDS_REVIEW";
}

/** Notice shown in the UI banner while the API is unavailable. */
export const UNREACHABLE_NOTICE = NOT_REACHABLE;
