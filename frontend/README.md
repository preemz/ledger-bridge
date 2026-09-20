# Ledger Bridge console (frontend)

Operator-facing console for Ledger Bridge: it watches accounting migrations run
and it is where the finance team works reconciliation breaks.

Stack: Next.js (App Router), TypeScript in strict mode, React, plain CSS in
`app/globals.css` (custom properties, no Tailwind, no component library).

## Requirements

- Node.js 20 or newer
- The Ledger Bridge API running separately (Django + DRF)

## Setup

```bash
npm install
cp .env.local.example .env.local   # optional, see below
npm run dev
```

The console is served at http://localhost:3000.

## Environment

| Variable               | Default                 | Purpose                        |
| ---------------------- | ----------------------- | ------------------------------ |
| `NEXT_PUBLIC_API_BASE` | `http://localhost:8000` | Base URL of the Ledger Bridge API |

`NEXT_PUBLIC_API_BASE` is read at build time, because it is inlined into the
browser bundle. Set it before `npm run build` for a production build.

## Scripts

| Command             | What it does                          |
| ------------------- | ------------------------------------- |
| `npm run dev`       | Development server with hot reload    |
| `npm run build`     | Production build                      |
| `npm start`         | Serve a production build              |
| `npm run lint`      | ESLint (next/core-web-vitals)         |
| `npm run typecheck` | `tsc --noEmit`                        |

## Routes

- `/` Operations dashboard: stats cards, a new migration form, and the migration
  job table. Polls `GET /api/stats/` and `GET /api/jobs/` every 2 seconds.
- `/jobs/[id]` Job detail: phase timeline (EXTRACT, STAGE, TRANSFORM, VALIDATE,
  LOAD, RECONCILE), counters, reconciliation run summary and breaks with an
  inline resolve control, and the audit log. Polls `GET /api/jobs/{id}/` every
  2 seconds and stops polling once the job is COMPLETED or FAILED.

## Layout

```
app/
  layout.tsx                shell: top bar and page frame
  page.tsx                  operations dashboard
  jobs/[id]/page.tsx        job detail
  globals.css               all styling, theme tokens in :root
components/                 presentational pieces and the two polling views
hooks/usePoll.ts            recursive-timer polling hook
lib/api.ts                  every API call, returns ApiResult, never throws
lib/format.ts               shared money, number, date and duration formatters
lib/types.ts                wire types for the API contract
```

## Behaviour when the API is down

No page crashes on a failed request. Each view keeps the last data it received,
shows a "Backend unreachable - retrying" notice, and keeps polling, so the
console recovers on its own once the API comes back. Empty and skeleton states
cover the case where nothing has loaded yet.
