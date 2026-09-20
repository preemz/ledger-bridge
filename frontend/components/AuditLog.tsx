import type { AuditEvent } from "@/lib/types";
import { formatTimestamp } from "@/lib/format";
import { EmptyState } from "./Notice";

/** Audit events, newest first. If the API already sorted them, keep that order. */
export function AuditLog({ events }: { events: AuditEvent[] }) {
  if (events.length === 0) {
    return <EmptyState>No audit events recorded for this job yet.</EmptyState>;
  }
  const ordered = [...events].sort((a, b) => {
    if (a.id !== b.id) return b.id - a.id;
    return Date.parse(b.created_at) - Date.parse(a.created_at);
  });
  return (
    <ul className="audit-list">
      {ordered.map((event) => (
        <li className="audit-item" key={`${event.id}-${event.created_at}`}>
          <span className="audit-time">{formatTimestamp(event.created_at)}</span>
          <span className="audit-type">{event.event_type}</span>
          <span className="audit-message">{event.message}</span>
          <span className="audit-actor">{event.actor}</span>
        </li>
      ))}
    </ul>
  );
}
