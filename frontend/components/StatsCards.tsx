import { formatInteger } from "@/lib/format";
import type { Stats } from "@/lib/types";
import { StatSkeleton } from "./Notice";

function StatCard({
  label,
  value,
  accent = false,
}: {
  label: string;
  value: number;
  accent?: boolean;
}) {
  return (
    <div className="stat-card">
      <div className="stat-label">{label}</div>
      <div className={accent ? "stat-value accent" : "stat-value"}>
        {formatInteger(value)}
      </div>
    </div>
  );
}

export function StatsCards({
  stats,
  loading,
}: {
  stats: Stats | null;
  loading: boolean;
}) {
  if (!stats) {
    return loading ? (
      <StatSkeleton />
    ) : (
      <div className="stat-grid">
        <StatCard label="Jobs total" value={0} />
        <StatCard label="Running" value={0} />
        <StatCard label="Completed" value={0} />
        <StatCard label="Breaks open" value={0} accent />
        <StatCard label="Entries loaded" value={0} />
      </div>
    );
  }
  return (
    <div className="stat-grid">
      <StatCard label="Jobs total" value={stats.jobs_total} />
      <StatCard label="Running" value={stats.jobs_running} />
      <StatCard label="Completed" value={stats.jobs_completed} />
      <StatCard label="Breaks open" value={stats.breaks_open} accent />
      <StatCard label="Accounts in ledger" value={stats.accounts_loaded} />
      <StatCard label="Entries in ledger" value={stats.entries_loaded} />
      <StatCard label="Lines in ledger" value={stats.lines_loaded} />
    </div>
  );
}
