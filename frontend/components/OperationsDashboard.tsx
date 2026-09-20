"use client";

import { useCallback, useState } from "react";
import { getJobs, getStats } from "@/lib/api";
import type { Job, Stats } from "@/lib/types";
import { usePoll } from "@/hooks/usePoll";
import { StatsCards } from "./StatsCards";
import { JobsTable } from "./JobsTable";
import { NewMigrationForm } from "./NewMigrationForm";
import { BackendNotice } from "./Notice";

const POLL_MS = 2000;

/** Dashboard body: stats, new migration form and the job table. Polls every 2s. */
export function OperationsDashboard({
  initialStats,
  initialJobs,
  initialError,
}: {
  initialStats: Stats | null;
  initialJobs: Job[];
  initialError: string | null;
}) {
  const [stats, setStats] = useState<Stats | null>(initialStats);
  const [jobs, setJobs] = useState<Job[]>(initialJobs);
  const [error, setError] = useState<string | null>(initialError);
  const [loaded, setLoaded] = useState(initialStats !== null || initialJobs.length > 0);

  const refresh = useCallback(async () => {
    const [statsResult, jobsResult] = await Promise.all([getStats(), getJobs()]);
    if (statsResult.ok) setStats(statsResult.data);
    if (jobsResult.ok) setJobs(jobsResult.data);
    let nextError: string | null = null;
    if (!statsResult.ok) {
      nextError = statsResult.error;
    } else if (!jobsResult.ok) {
      nextError = jobsResult.error;
    }
    setError(nextError);
    setLoaded(true);
  }, []);

  usePoll(refresh, POLL_MS);

  return (
    <>
      <BackendNotice error={error} />
      <StatsCards stats={stats} loading={!loaded} />
      <section className="section">
        <div className="section-head">
          <h2 className="section-title">New migration</h2>
          <span className="section-note">
            Extracts the chart of accounts, opening balances and journal entries
          </span>
        </div>
        <div className="panel panel-pad">
          <NewMigrationForm datasetAsOfDate={stats?.dataset_as_of_date ?? null} />
        </div>
      </section>
      <section className="section">
        <div className="section-head">
          <h2 className="section-title">Migration jobs</h2>
          <span className="section-note">Refreshing every 2 seconds</span>
        </div>
        <div className="panel">
          <JobsTable jobs={jobs} loading={!loaded} />
        </div>
      </section>
    </>
  );
}