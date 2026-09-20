// Small presentational primitives shared by the server shell and the
// polling client components.

import type { ReactNode } from "react";

export type NoticeTone = "danger" | "warn" | "ok" | "info";

export function Notice({
  tone = "info",
  children,
}: {
  tone?: NoticeTone;
  children: ReactNode;
}) {
  const cls =
    tone === "danger"
      ? "banner banner-danger"
      : tone === "warn"
        ? "banner banner-warn"
        : tone === "ok"
          ? "banner banner-ok"
          : "banner";
  return (
    <div className={cls} role="status">
      <span className="banner-token" aria-hidden="true" />
      <span>{children}</span>
    </div>
  );
}

/** Shown whenever the last request failed. Data already on screen stays. */
export function BackendNotice({ error }: { error: string | null }) {
  if (!error) return null;
  return <Notice tone="danger">{error}</Notice>;
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <div className="empty-state">{children}</div>;
}

export function StatSkeleton() {
  return (
    <div className="stat-grid">
      {[0, 1, 2, 3, 4].map((i) => (
        <div className="stat-card" key={i}>
          <div className="skeleton skeleton-line" style={{ width: "55%" }} />
          <div className="skeleton skeleton-card" style={{ height: 22, marginTop: 10 }} />
        </div>
      ))}
    </div>
  );
}

export function TableSkeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div className="table-wrap">
      <table className="data">
        <tbody>
          {Array.from({ length: rows }, (_, i) => (
            <tr className="skeleton-row" key={i}>
              <td colSpan={7}>
                <div className="skeleton skeleton-cell" />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
