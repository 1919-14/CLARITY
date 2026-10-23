# CLARITY Research Dashboard (Issue #21)

The React interface is a read-mostly window onto the Flask API. It configures
runs, reports lifecycle state, renders resource accounting, and exposes the
scientific records (hypotheses, reports, benchmarks) the backend persisted. It
does not compute science, invent identifiers, or fabricate metrics in the
browser.

## Pages and routes

| Route | Page | Backend reads |
|---|---|---|
| `/` | Research dashboard | `GET /api/v1/runs`; the active run panel adds `GET /runs/<id>`, `GET /runs/<id>/status`, `GET /runs/<id>/budget` |
| `/runs/new` | Run configuration | `POST /api/v1/runs` |
| `/runs/:runId` | Run ledger | `GET /runs/<id>`, `GET /runs/<id>/status`, `GET /runs/<id>/budget`; `POST /runs/<id>/cancel` |
| `/hypotheses` | Hypothesis register | `GET /runs/<id>/hypotheses?include_excluded=true` |
| `/reports` | Scientific report | `GET /runs/<id>/report`; `POST /runs/<id>/report` to request generation |
| `/benchmarks` | Benchmark register | `GET /benchmarks/scenarios`, `GET /runs/<id>/benchmarks` |
| any other | Not found | none |

`/hypotheses`, `/reports`, and `/benchmarks` accept a `?run=<run_id>` query
parameter and keep it in sync with the run picker, so a view can be linked
directly. When no run is selected the newest persisted run is used; when no run
exists at all the page explains that the view belongs to a run instead of
showing an empty table.

## States the interface distinguishes

- **Loading** – a first read is in flight.
- **Refreshing** – a deliberate re-read is in flight; previous values stay on
  screen.
- **Empty** – the backend answered successfully but has no records. This is
  shown as an honest absence, never as a zero score.
- **Error** – HTTP, network, timeout, or malformed-response failure. The error
  retains the API base URL and the HTTP status so the operator can act on it.
- **Unavailable** – the backend returned `null`/`undefined` for a value. It is
  rendered as “Unavailable”, never as `0`, `$0`, or a made-up number.
- **Stale** – a refresh failed while a run ledger was already on screen; the
  last known values remain visible with a warning and polling continues.
- **Ambiguous submission** – the create-run request timed out. The form refuses
  to resubmit until the researcher checks the run archive, because a run may
  have been persisted.

## Client architecture

- `src/api/client.js` is the only module that calls `fetch`. It owns the base
  URL, JSON encoding/decoding, a 15-second default timeout, caller aborts, and
  normalisation into `ApiError` (`http`, `network`, `timeout`, `aborted`,
  `malformed`). Field-level validation details are exposed via
  `ApiError.fieldErrors`.
- `src/api/runs.js` wraps each endpoint; `src/api/contracts.js` documents the
  response shapes and run-state sets (`TERMINAL_RUN_STATES`,
  `CANCELLABLE_RUN_STATES`, `ACTIVE_RUN_STATES`).
- `src/hooks/useRuns.js`, `useResource.js`, and `useRunStatus.js` own loading,
  aborting, and polling. `useRunStatus` polls every 4 seconds only while a run
  is non-terminal, keeps at most one request batch in flight, stops on 404, and
  tears the interval down on unmount or run change.
- `src/utils/validation.js` mirrors the backend budget bounds so the form gives
  fast feedback. The backend re-validates and remains the authority.
- `src/utils/formatting.js` centralises UTC date formatting, decimals,
  durations, IDs, and the “Unavailable” rule.

## Configuration

The API base URL is a Vite build-time variable and **must include the `/api/v1`
namespace**:

```bash
# frontend/.env
VITE_API_BASE_URL=http://127.0.0.1:5000/api/v1
VITE_API_TIMEOUT_MS=15000
```

The default in `src/api/client.js` is
`http://127.0.0.1:5000/api/v1`. A bare origin (`http://127.0.0.1:5000`) makes
requests to `/runs` instead of `/api/v1/runs` and produces 404s, so keep the
namespace in any override. The footer of every page prints the resolved base URL
to make a misconfiguration immediately visible.

## Running and verifying locally

```bash
# Once (and after pulling new migrations): create the local SQLite schema.
python -m alembic upgrade head

# Terminal 1 — backend (serves backend/clarity_dev.db)
python backend/run.py

# Terminal 2 — interface
cd frontend
npm ci
npm test
npm run lint
npm run build
npm run dev          # http://localhost:5173
```

The backend must allow the dev origin (`http://127.0.0.1:5173`); the default
development CORS configuration already does.

Contract smoke check without a browser (Node 18+):

```bash
cd frontend
node --input-type=module -e "
import { listRuns, getRunStatus, getRunBudget } from './src/api/runs.js';
const runs = await listRuns({ limit: 1 });
const id = runs.runs[0].run_id;
console.log(await getRun(id), await getRunStatus(id), await getRunBudget(id));
"
```

This was executed against a locally running Flask API on the seeded
`clarity_dev.db`: list, detail, status, budget, hypotheses, report (pending),
benchmarks, and benchmark scenarios all returned the shapes the interface
expects. No remote Nodus calls were involved.

## Automated tests

`npm test` runs Vitest in jsdom with Testing Library. The suite covers:

- API client behaviour: success, HTTP errors, network failure, timeout,
  malformed JSON, and caller abort (`src/api/client.test.js`).
- Formatting and unavailability rules (`src/utils/formatting.test.js`).
- Run-form validation bounds and payload construction
  (`src/utils/validation.test.js`).
- Budget panel rendering, including cost states and limits
  (`src/components/status/BudgetPanel.test.jsx`).
- Dashboard, create-run, and run-ledger pages across loading, empty, error,
  success, stale, and cancellation states
  (`src/pages/*.test.jsx`).
- Hypotheses, reports, and benchmarks workflows
  (`src/pages/ScientificWorkflows.test.jsx`).
- Routing and navigation shell (`src/App.test.jsx`).

CI runs `npm test` alongside the lint and production-build steps.

## Deliberate limitations

- The interface is read-only for hypothesis pinning, exclusion, re-evaluation,
  and revision editing; those researcher actions remain API-only for now.
- Usage/audit history and report export are not surfaced.
- Live Nodus execution and remote benchmarking remain pending credentials;
  nothing in the interface claims remote task IDs, costs, or speedups.
- There is no authentication layer, streaming transport, or client-side cache.
  Progress is polling, and every page reads from the backend on load.
- Timestamps are displayed in UTC with an explicit suffix; local-time display is
  not configurable.
- Run creation does not start discovery; the backend lifecycle manager remains
  responsible for execution.
