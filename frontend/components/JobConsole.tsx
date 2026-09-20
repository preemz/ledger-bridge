"use client";

import Link from "next/link";
import { useCallback, useRef, useState } from "react";
import { getAudit, getJob, sendSignal } from "@/lib/api";
import type { AuditEvent, Break, JobStatus, JobWithDetail, Signal } from "@/lib/types";
import { isTerminalStatus } from "@/lib/api";
import { formatDuration, formatTimestamp } from "@/lib/format";
import { usePoll } from "@/hooks/usePoll";
import { useMounted } from "@/hooks/useMounted";
import { StatusPill } from "./StatusPill";
import { PhaseTimeline } from "./PhaseTimeline";
import { CountersStrip } from "./CountersStrip";
import { AuditLog } from "./AuditLog";
import { ReconciliationBreaks, ReconciliationSummary } from "./Reconciliation";
import { BackendNotice, EmptyState, Notice } from "./Notice";

const POLL_MS = 2000;

const SIGNAL_LABEL: Record<Signal, string> = {
  pause: "Pause",
  resume: "Resume",
  cancel: "Cancel",
};

function signalAllowed(signal: Signal, status: JobStatus | null): boolean {
  if (status === null) return false;
  if (isTerminalStatus(status)) return false;
  if (signal === "pause") return status === "RUNNING" || status === "PENDING";
  if (signal === "resume") return status === "PAUSED";
  return true;
}

/** Why a signal button is unavailable, so a disabled control explains itself. */
function signalBlockedReason(signal: Signal, status: JobStatus | null): string | null {
  if (status === null) return "Waiting for the first poll from the API.";
  if (signalAllowed(signal, status)) return null;
  if (isTerminalStatus(status)) {
    return `The job has finished (${status}), so it cannot be signalled.`;
  }
  if (signal === "pause") return "Pause is available while the job is pending or running.";
  if (signal === "resume") return "Resume is available while the job is paused.";
  return null;
}

/** Job detail console. Polls every 2 seconds until the job reaches a terminal status. */
export function JobConsole({
  jobId,
  initialJob,
  initialAudit,
  initialError,
}: {
  jobId: string;
  initialJob: JobWithDetail | null;
  initialAudit: AuditEvent[];
  initialError: string | null;
}) {
  const [job, setJob] = useState<JobWithDetail | null>(initialJob);
  const [audit, setAudit] = useState<AuditEvent[]>(initialAudit);
  const mounted = useMounted();
  const [error, setError] = useState<string | null>(initialError);
  const [auditError, setAuditError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionNote, setActionNote] = useState<string | null>(null);
  const [pendingSignal, setPendingSignal] = useState<Signal | null>(null);
  const [breaks, setBreaks] = useState<Break[]>(
    initialJob?.reconciliation?.breaks ?? [],
  );

  // Locally resolved ids survive a stale poll response.
  const resolvedIds = useRef<Set<string>>(new Set(initialJob?.reconciliation?.breaks.filter((b) => b.resolved).map((b) => b.id) ?? []));

  const applyBreaks = useCallback((incoming: Break[]) => {
    setBreaks(
      incoming.map((row) =>
        resolvedIds.current.has(row.id) ? { ...row, resolved: true } : row,
      ),
    );
  }, []);

  const refresh = useCallback(async () => {
    const jobResult = await getJob(jobId);
    if (jobResult.ok) {
      setJob(jobResult.data);
      applyBreaks(jobResult.data.reconciliation?.breaks ?? []);
    }
    setError(jobResult.ok ? null : jobResult.error);

    const auditResult = await getAudit(jobId);
    if (auditResult.ok) setAudit(auditResult.data);
    setAuditError(auditResult.ok ? null : auditResult.error);
  }, [jobId, applyBreaks]);

  // Declared after refresh on purpose: the dependency array is evaluated here, and
  // referencing refresh above its own declaration would be a temporal dead zone error.
  const handleResolved = useCallback(
    (updated: Break) => {
      resolvedIds.current.add(updated.id);
      setBreaks((prev) => prev.map((row) => (row.id === updated.id ? updated : row)));
      setActionNote(`Break on account ${updated.account_code} resolved.`);
      // Refresh once, because on a terminal job polling has stopped and the audit log
      // would otherwise never show the break.resolved event this action just created.
      // An operator action should not leave the evidence for it stale on screen.
      void refresh();
    },
    [refresh],
  );

  const terminal = job !== null && isTerminalStatus(job.status);
  usePoll(refresh, POLL_MS, !terminal);

  async function handleSignal(signal: Signal) {
    setPendingSignal(signal);
    setActionError(null);
    setActionNote(null);
    const result = await sendSignal(jobId, signal);
    setPendingSignal(null);
    if (!result.ok) {
      setActionError(result.error);
      return;
    }
    // HTTP 200 is not success. The API answers 200 with ok:false when the workflow
    // refuses the signal, and detail carries the reason worth showing the operator.
    if (!result.data.ok) {
      setActionError(
        result.data.detail ||
          `The workflow did not accept the ${SIGNAL_LABEL[signal].toLowerCase()} signal.`,
      );
      return;
    }
    setActionNote(
      `${SIGNAL_LABEL[signal]} accepted. Job status is now ${result.data.status}.`,
    );
    await refresh();
  }

  const status: JobStatus | null = job ? job.status : null;
  const notFound = error !== null && error.includes("404");

  return (
    <>
      <header className="page-header">
        <div>
          <h1 className="page-title">
            {job?.customer_reference ? job.customer_reference : `Job ${jobId}`}
          </h1>
          <p className="page-subtitle">
            <span className="mono">{job?.source_system || "unknown source"}</span>
            {" / "}
            <span className="mono">
              {job?.temporal_workflow_id || "no workflow id"}
            </span>
            {" / created "}
            {formatTimestamp(job?.created_at ?? null)}
          </p>
        </div>
        <div className="page-actions">
          {job ? <StatusPill token={job.status} /> : null}
          {(["pause", "resume", "cancel"] as Signal[]).map((signal) => (
            <button
              key={signal}
              type="button"
              className={signal === "cancel" ? "btn btn-danger" : "btn"}
              disabled={!signalAllowed(signal, status) || pendingSignal !== null}
              title={
                signalBlockedReason(signal, status) ??
                `Send the ${signal} signal to the workflow.`
              }
              onClick={() => {
                void handleSignal(signal);
              }}
            >
              {pendingSignal === signal ? "Sending" : SIGNAL_LABEL[signal]}
            </button>
          ))}
          <Link className="btn" href="/">
            Back
          </Link>
        </div>
      </header>

      {notFound ? (
        <Notice tone="danger">Job not found on the API (404).</Notice>
      ) : (
        <BackendNotice error={error} />
      )}
      {actionError ? <Notice tone="danger">{actionError}</Notice> : null}
      {actionNote ? <Notice tone="ok">{actionNote}</Notice> : null}
      {terminal ? (
        <Notice tone="info">
          Job reached {job?.status}. Polling stopped.
        </Notice>
      ) : null}
      {job?.error ? <Notice tone="danger">{job.error}</Notice> : null}

      <section className="section">
        <div className="section-head">
          <h2 className="section-title">Phase timeline</h2>
          <span className="section-note">
            Elapsed{" "}
            {mounted || job?.finished_at
              ? formatDuration(job?.started_at ?? null, job?.finished_at ?? null)
              : "-"}
          </span>
        </div>
        <div className="panel panel-pad">
          {job ? (
            <PhaseTimeline phases={job.phases} />
          ) : (
            <EmptyState>Phase data unavailable while the API is unreachable.</EmptyState>
          )}
        </div>
      </section>

      <section className="section">
        <div className="section-head">
          <h2 className="section-title">Counters</h2>
          <span className="section-note">Missing values reported as 0</span>
        </div>
        {job ? (
          <CountersStrip counters={job.counters} />
        ) : (
          <div className="panel">
            <EmptyState>No counters to show.</EmptyState>
          </div>
        )}
      </section>

      <section className="section">
        <div className="section-head">
          <h2 className="section-title">Reconciliation</h2>
          <span className="section-note">
            {job?.reconciliation
              ? `Run ${job.reconciliation.run.id}`
              : "Waiting for the load phase to finish"}
          </span>
        </div>
        {job?.reconciliation ? (
          <>
            <ReconciliationSummary run={job.reconciliation.run} />
            <div className="section-head" style={{ marginTop: 16 }}>
              <h3 className="section-title">Reconciliation breaks</h3>
            </div>
            <div className="panel">
              <ReconciliationBreaks breaks={breaks} onResolved={handleResolved} />
            </div>
          </>
        ) : (
          <div className="panel">
            <EmptyState>
              No reconciliation run for this job yet. Breaks appear once the
              reconcile phase completes.
            </EmptyState>
          </div>
        )}
      </section>

      <section className="section">
        <div className="section-head">
          <h2 className="section-title">Audit log</h2>
          <span className="section-note">Newest first</span>
        </div>
        {auditError ? (
          <BackendNotice error={auditError} />
        ) : null}
        <div className="panel">
          <AuditLog events={audit} />
        </div>
      </section>
    </>
  );
}