import type { Metadata } from "next";
import { JobConsole } from "@/components/JobConsole";
import { getAudit, getJob } from "@/lib/api";
import type { AuditEvent, JobWithDetail } from "@/lib/types";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Job - Ledger Bridge console",
};

export default async function JobPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const [jobResult, auditResult] = await Promise.all([getJob(id), getAudit(id)]);
  const job: JobWithDetail | null = jobResult.ok ? jobResult.data : null;
  const audit: AuditEvent[] = auditResult.ok ? auditResult.data : [];

  return (
    <JobConsole
      jobId={id}
      initialJob={job}
      initialAudit={audit}
      initialError={jobResult.ok ? null : jobResult.error}
    />
  );
}