import { OperationsDashboard } from "@/components/OperationsDashboard";
import { getJobs, getStats } from "@/lib/api";
import type { Job, Stats } from "@/lib/types";

// The console reflects live operational state, so nothing here is prerendered.
export const dynamic = "force-dynamic";

export default async function OperationsPage() {
  const [statsResult, jobsResult] = await Promise.all([getStats(), getJobs()]);
  const stats: Stats | null = statsResult.ok ? statsResult.data : null;
  const jobs: Job[] = jobsResult.ok ? jobsResult.data : [];
  const initialError = !statsResult.ok
    ? statsResult.error
    : !jobsResult.ok
      ? jobsResult.error
      : null;

  return (
    <>
      <header className="page-header">
        <div>
          <h1 className="page-title">Operations</h1>
          <p className="page-subtitle">
            Ledger Bridge migration and reconciliation console
          </p>
        </div>
      </header>
      <OperationsDashboard
        initialStats={stats}
        initialJobs={jobs}
        initialError={initialError}
      />
    </>
  );
}