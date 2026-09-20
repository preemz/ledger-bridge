// Wire types for the Ledger Bridge API. These mirror the contract served by the
// Django + DRF backend exactly. Money values arrive as decimal strings.

export type JobStatus =
  | "PENDING"
  | "RUNNING"
  | "PAUSED"
  | "COMPLETED"
  | "FAILED"
  | "NEEDS_REVIEW";

export type PhaseStatus =
  | "PENDING"
  | "RUNNING"
  | "COMPLETED"
  | "FAILED"
  | "SKIPPED";

export type BreakClassification =
  | "UNCLASSIFIED"
  | "ROUNDING"
  | "TIMING"
  | "FX"
  | "MAPPING_ERROR"
  | "TRUE_BREAK";

export type BreakSeverity = "LOW" | "MEDIUM" | "HIGH";

export type SourceSystem = "legacy_erp" | "netsuite" | "xero";

export type Signal = "pause" | "resume" | "cancel";

/**
 * Response from POST /api/jobs/{id}/signal/.
 *
 * The endpoint answers HTTP 200 whether or not the workflow accepted the signal, and
 * says why in `detail`. Treating 200 as success would report "Pause accepted" for a job
 * the API just refused to pause, for example one running on the inline runner, which
 * cannot be signalled at all.
 */
export interface SignalResponse {
  ok: boolean;
  signal: Signal;
  status: JobStatus;
  detail: string;
}

/** Every counter is optional on the wire. Missing keys render as 0. */
export interface Counters {
  rows_read?: number;
  rows_staged?: number;
  rows_loaded?: number;
  rows_skipped?: number;
  entries_loaded?: number;
  lines_loaded?: number;
  breaks_found?: number;
}

export interface Job {
  id: string;
  customer_reference: string;
  source_system: string;
  status: JobStatus;
  temporal_workflow_id: string | null;
  counters: Counters;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  error: string | null;
}

export interface Phase {
  /** Present on rows returned by the API. Optional so an empty placeholder compiles. */
  id?: string;
  name: string;
  status: PhaseStatus;
  attempt: number;
  /** Which slice of the phase this row is, for phases split into retryable chunks. */
  chunk: number;
  /** Discriminates two kinds of work sharing a phase name, for example "accounts". */
  label: string;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  /** Rendered by the timeline, so values arrive pre-stringified. */
  stats: Record<string, string>;
  error: string | null;
}

export interface ReconciliationRun {
  id: string;
  job: string;
  as_of_date: string;
  status: string;
  legacy_total_debit: string;
  legacy_total_credit: string;
  loaded_total_debit: string;
  loaded_total_credit: string;
  legacy_entry_count: number;
  loaded_entry_count: number;
  difference: string;
  created_at: string;
}

export interface Break {
  id: string;
  run: string;
  account_code: string;
  account_name: string;
  legacy_balance: string;
  loaded_balance: string;
  variance: string;
  /** Position when accounts are ranked by absolute variance. 1 is the largest break. */
  variance_rank: number;
  /** Share of the total unexplained amount, this break and all larger ones together. */
  cumulative_variance_pct: string;
  classification: BreakClassification;
  severity: BreakSeverity;
  resolved: boolean;
  note: string;
}

export interface JobWithDetail extends Job {
  phases: Phase[];
  reconciliation: { run: ReconciliationRun; breaks: Break[] } | null;
}

export interface Stats {
  jobs_total: number;
  jobs_running: number;
  jobs_completed: number;
  breaks_open: number;
  /** Counted from the ledger tables, so this is what is in the ledger, not a sum of jobs. */
  entries_loaded: number;
  lines_loaded: number;
  accounts_loaded: number;
  /** Cutover date of the seeded dataset, used as the default for a new migration. */
  dataset_as_of_date: string | null;
}

export interface AuditEvent {
  id: number;
  event_type: string;
  actor: string;
  message: string;
  payload: unknown;
  created_at: string;
}

export interface NewJobInput {
  customer_reference: string;
  source_system: SourceSystem;
  as_of_date: string;
}

export interface ResolveInput {
  classification: BreakClassification;
  note: string;
}

/** Phase names rendered in the job timeline, in pipeline order. */
export const PHASE_ORDER: readonly string[] = [
  "EXTRACT",
  "STAGE",
  "TRANSFORM",
  "VALIDATE",
  "LOAD",
  "RECONCILE",
];

export const SOURCE_SYSTEMS: readonly SourceSystem[] = [
  "legacy_erp",
  "netsuite",
  "xero",
];

export const BREAK_CLASSIFICATIONS: readonly BreakClassification[] = [
  "ROUNDING",
  "TIMING",
  "FX",
  "MAPPING_ERROR",
  "TRUE_BREAK",
];

export const COUNTER_LABELS: ReadonlyArray<[keyof Counters, string]> = [
  ["rows_read", "Rows read"],
  ["rows_staged", "Rows staged"],
  ["rows_loaded", "Rows loaded"],
  ["rows_skipped", "Rows skipped"],
  ["entries_loaded", "Entries loaded"],
  ["lines_loaded", "Lines loaded"],
  ["breaks_found", "Breaks found"],
];
