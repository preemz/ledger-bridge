import { humanizeToken } from "@/lib/format";

const TONE_BY_TOKEN: Record<string, string> = {
  PENDING: "pill",
  RUNNING: "pill pill-running",
  PAUSED: "pill pill-paused",
  COMPLETED: "pill pill-completed",
  FAILED: "pill pill-failed",
  NEEDS_REVIEW: "pill pill-needs_review",
  SKIPPED: "pill pill-skipped",
  HIGH: "pill pill-failed",
  MEDIUM: "pill pill-paused",
  LOW: "pill",
  UNCLASSIFIED: "pill",
  ROUNDING: "pill pill-running",
  TIMING: "pill pill-paused",
  FX: "pill pill-running",
  MAPPING_ERROR: "pill pill-paused",
  TRUE_BREAK: "pill pill-failed",
};

export function StatusPill({ token, label }: { token: string; label?: string }) {
  const key = token.toUpperCase();
  const cls = TONE_BY_TOKEN[key] ?? "pill";
  return (
    <span className={cls} title={key}>
      <span className="pill-dot" aria-hidden="true" />
      {label ?? humanizeToken(key)}
    </span>
  );
}
