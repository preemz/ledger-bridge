"use client";

import type { Phase } from "@/lib/types";
import { PHASE_ORDER } from "@/lib/types";
import { formatDurationMs, formatTimestamp, humanizeToken } from "@/lib/format";
import { useMounted } from "@/hooks/useMounted";

function nodeClass(status: Phase["status"]): string {
  switch (status) {
    case "RUNNING":
      return "phase-node phase-node-running";
    case "COMPLETED":
      return "phase-node phase-node-completed";
    case "FAILED":
      return "phase-node phase-node-failed";
    case "SKIPPED":
      return "phase-node phase-node-skipped";
    default:
      return "phase-node";
  }
}

function nodeGlyph(status: Phase["status"]): string {
  switch (status) {
    case "COMPLETED":
      return "ok";
    case "FAILED":
      return "!";
    case "SKIPPED":
      return "-";
    case "RUNNING":
      return "..";
    default:
      return "";
  }
}

function emptyPhase(name: string): Phase {
  return {
    name,
    status: "PENDING",
    attempt: 0,
    chunk: 0,
    label: "",
    started_at: null,
    finished_at: null,
    duration_ms: null,
    stats: {},
    error: null,
  };
}

function rowLabel(phase: Phase): string {
  const suffix = phase.label && !phase.label.startsWith("entries") ? ` ${phase.label}` : "";
  return `#${phase.attempt}.${phase.chunk}${suffix}`;
}

function statsLine(stats: Phase["stats"]): string {
  const entries = Object.entries(stats);
  if (entries.length === 0) return "";
  const shown = entries.slice(0, 4).map(([key, value]) => `${key}: ${value}`);
  const suffix = entries.length > 4 ? `, +${entries.length - 4} more` : "";
  return shown.join(", ") + suffix;
}

/** Horizontal pipeline view, one node per phase, with every chunk and attempt listed.

 * The API returns one row per phase chunk, and a long phase is many chunks. Showing only
 * the last row per phase name hid the two things the timeline exists to show: the chunk
 * that a killed worker abandoned, and the retry that finished it. Every row is rendered,
 * in order, so an interrupted phase reads as an interruption.
 */
export function PhaseTimeline({ phases }: { phases: Phase[] }) {
  const mounted = useMounted();

  const byName = new Map<string, Phase[]>();
  for (const phase of phases) {
    const key = phase.name.toUpperCase();
    byName.set(key, [...(byName.get(key) ?? []), phase]);
  }
  for (const [key, rows] of byName) {
    byName.set(
      key,
      [...rows].sort((a, b) => a.attempt - b.attempt || a.chunk - b.chunk),
    );
  }

  const names = [
    ...PHASE_ORDER,
    ...phases
      .map((p) => p.name.toUpperCase())
      .filter((name) => name && !PHASE_ORDER.includes(name)),
  ];

  const groups = names.map((name) => {
    const rows = byName.get(name) ?? [];
    const latest: Phase = rows.length > 0 ? (rows[rows.length - 1] as Phase) : emptyPhase(name);
    const retried = rows.filter((row) => row.status === "FAILED").length;
    const stillRunning = rows.filter((row) => row.status === "RUNNING").length;
    const totalMs = rows.reduce((sum, row) => sum + (row.duration_ms ?? 0), 0);
    return { name, rows, latest, retried, stillRunning, totalMs };
  });

  return (
    <div className="timeline">
      {groups.map((group) => (
        <div className="phase" key={group.name}>
          <div className={nodeClass(group.latest.status)}>{nodeGlyph(group.latest.status)}</div>
          <div className="phase-body">
            <div className="phase-name">{group.name}</div>
            <div className="phase-status">
              {humanizeToken(group.latest.status)}
              {group.retried > 0 ? (
                <span className="phase-badge" title="an earlier attempt did not finish and was retried">
                  retried
                </span>
              ) : null}
              {group.stillRunning > 1 ? (
                <span className="phase-badge phase-badge-warn">
                  {group.stillRunning} running
                </span>
              ) : null}
            </div>
            <div className="phase-meta">
              {group.rows.length || 1} {group.rows.length === 1 ? "step" : "steps"} /{" "}
              {mounted || group.latest.finished_at ? formatDurationMs(group.totalMs) : "-"}
            </div>

            {group.rows.length === 0 ? (
              <div className="phase-meta">{formatTimestamp(null)}</div>
            ) : (
              <div className="phase-rows">
                {group.rows.map((row) => {
                  const stats = statsLine(row.stats);
                  return (
                    <div
                      className={
                        row.status === "FAILED"
                          ? "phase-row phase-row-failed"
                          : row.status === "RUNNING"
                            ? "phase-row phase-row-running"
                            : "phase-row"
                      }
                      key={row.id ?? `${row.attempt}-${row.chunk}-${row.label}`}
                    >
                      <span className="phase-row-head">
                        {rowLabel(row)} {humanizeToken(row.status)}
                        {row.duration_ms !== null && row.duration_ms !== undefined
                          ? ` ${formatDurationMs(row.duration_ms)}`
                          : ""}
                      </span>
                      {row.error ? (
                        <span className="phase-error" title={row.error}>
                          {row.error}
                        </span>
                      ) : null}
                      {stats ? (
                        <span className="phase-row-stats" title={stats}>
                          {stats}
                        </span>
                      ) : null}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
