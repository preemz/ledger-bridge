import Link from "next/link";
import type { Job } from "@/lib/types";
import { formatInteger, formatTimestamp } from "@/lib/format";
import { StatusPill } from "./StatusPill";
import { EmptyState, TableSkeleton } from "./Notice";

export function JobsTable({
  jobs,
  loading,
}: {
  jobs: Job[];
  loading: boolean;
}) {
  if (loading && jobs.length === 0) {
    return <TableSkeleton rows={4} />;
  }
  if (jobs.length === 0) {
    return <EmptyState>No migration jobs yet. Create one to start an extract.</EmptyState>;
  }
  return (
    <div className="table-wrap">
      <table className="data">
        <thead>
          <tr>
            <th>Customer reference</th>
            <th>Source system</th>
            <th>Status</th>
            <th className="num">Rows read</th>
            <th className="num">Entries loaded</th>
            <th className="num">Breaks found</th>
            <th>Created</th>
            <th>Job</th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => (
            <tr key={job.id}>
              <td>{job.customer_reference || "-"}</td>
              <td className="row-muted mono">{job.source_system || "-"}</td>
              <td>
                <StatusPill token={job.status} />
              </td>
              <td className="num">{formatInteger(job.counters.rows_read ?? 0)}</td>
              <td className="num">{formatInteger(job.counters.entries_loaded ?? 0)}</td>
              <td className="num">{formatInteger(job.counters.breaks_found ?? 0)}</td>
              <td className="row-muted mono">{formatTimestamp(job.created_at)}</td>
              <td>
                <Link className="row-link" href={`/jobs/${job.id}`}>
                  Open
                </Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
