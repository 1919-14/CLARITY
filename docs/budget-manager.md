# Per-Run Budget Manager (Issue #14)

Every discovery run has an explicit, validated resource budget. The budget
manager stops **admitting** work when a limit is reached, preserves completed
results, and records a structured reason for stopping. It is a persistence-backed
service (`backend/app/services/budget_service.py`) whose contract lives in
`backend/app/domain/budget_contracts.py`; it is not a frontend or route-level
validation helper.

> **Enforcement honesty.** Oracle, evaluation, hypothesis, concurrency, and
> runtime limits are enforced by this application against its own database.
> `nodus-compute 1.0.27` exposes no verified pre-execution cost estimate, no
> per-run spending cap, and no settled billing callback. The compute-spend
> ceiling is therefore an **application-side estimate gate**: it stops future
> scheduling and is never a guaranteed provider maximum charge.

---

## 1. Configuration fields, defaults, and zero semantics

| Field | Type | Default | Zero means | Omitted means |
|---|---|---|---|---|
| `max_oracle_queries` | int ≥ 0 | `10` | no oracle query may be admitted | default `10` |
| `max_candidate_evaluations` | int ≥ 0 | `100` | no candidate evaluation may be admitted | default `100` |
| `max_concurrent_remote_tasks` | int ≥ 0 | `4` | no remote task may be active | default `4` |
| `max_runtime_seconds` | int ≥ 0 | `3600` | no execution time; no work may start | default `3600` |
| `max_generated_hypotheses` | int ≥ 0 or `null` | `null` | no new hypothesis may be generated | disabled (no limit) |
| `max_compute_spend_usd` | finite number ≥ 0 or `null` | `null` | no positive-cost work may be admitted | disabled (no limit) |

Zero is **never** unlimited. A `null` optional limit means *disabled*, which is
different from zero, omitted, unknown, and unlimited.

Bounds: queries ≤ 100,000; evaluations ≤ 1,000,000; concurrency ≤ 32 (matching
the parallel-evaluation ceiling); runtime ≤ 604,800 s (7 days); hypotheses ≤
10,000; spend ≤ $10,000. Values outside these bounds are rejected.

### Rejected configurations

Negative limits, booleans where integers are expected, fractional values for
integer-only limits, NaN/∞ money, malformed budget objects (non-object/mapping),
unknown field names, duplicate/conflicting canonical and legacy fields, and values
outside the documented bounds all return `400` with per-field details:

```json
{
  "error": "Bad Request",
  "message": "Invalid run budget configuration.",
  "status_code": 400,
  "details": [
    {
      "field": "max_oracle_queries",
      "code": "OUT_OF_RANGE",
      "message": "'max_oracle_queries' must be >= 0.",
      "received": -1
    }
  ]
}
```

### Configuration at creation

`POST /api/v1/runs` accepts either a canonical nested object or the legacy flat
v1 fields. Mixing both is rejected as conflicting.

```json
{
  "name": "Budgeted polynomial run",
  "execution_mode": "local",
  "budget": {
    "max_oracle_queries": 25,
    "max_candidate_evaluations": 300,
    "max_concurrent_remote_tasks": 2,
    "max_runtime_seconds": 900,
    "max_generated_hypotheses": 40,
    "max_compute_spend_usd": 1.5
  }
}
```

Legacy equivalent (still supported):

```json
{
  "name": "Legacy payload",
  "max_queries": 25,
  "max_evaluations": 300,
  "max_runtime_seconds": 900,
  "spend_ceiling_usd": 1.5
}
```

The budget is immutable once the run exists. Initializing it again with a
different configuration raises `INVALID_BUDGET`/`IMMUTABLE_BUDGET` rather than
silently widening a limit. Legacy runs created before this feature are resolved
lazily from their existing columns, with omitted values taking the documented
defaults.

---

## 2. Accounting rules

### Oracle queries

A query attempt is charged **when it is admitted**, before the oracle call:

```
queries_reserved  = admitted invocation attempts (including in-flight)
queries_completed = attempts that returned a validated success
queries_failed    = attempts that raised or returned an invalid/failed response
queries_released  = admitted attempts provably never dispatched; capacity returned
```

* A proposed experiment that is not executed is **not** a query. Pre-invocation
  validation rejections and duplicate-input skips are recorded as proposal
  diagnostics, not as oracle attempts.
* Every retry that reaches the oracle is a new attempt and is charged again.
  A retry denied by the budget never reaches the oracle.
* A timeout whose outcome is unknown is recorded as `ambiguous` and **stays
  charged**. The manager never assumes a remote call did not execute.
* Capacity is released only when the caller can prove the invocation was never
  dispatched (`released`).

### Candidate evaluations

Each admitted evaluation attempt consumes one unit. The parallel evaluation
runner reserves the whole batch before dispatch; a batch-level retry of the same
logical evaluation does not re-reserve, while a later `run()`/refresh does.
Completed and failed evaluations are tracked separately.

### Generated hypotheses

Each newly adopted hypothesis entity consumes one unit. Revisions of an existing
hypothesis do not. Invalid, duplicate, or rejected proposals do not consume the
budget: the engine reserves only proposals it will actually adopt.

### Idempotency and replay

Every reservation has a unique `(run_id, resource, operation_id)` key. Duplicate
worker delivery of the same logical operation never consumes capacity twice.
Operation IDs are stable per logical attempt (for example `run-123:query:7`), so
a crash-retry cannot silently double-charge.

Idempotent accounting is not the same as re-authorization. A reservation row
records that an operation was admitted **once**; it never proves that the
underlying oracle call, evaluation, or provider task may run again. A duplicate
delivery in any state — `reserved`, `committed`, `released`, or `ambiguous` —
raises `BudgetReservationReplay` and does not authorize a second execution:

* `DiscoveryEngine` refuses the query and records `DUPLICATE_BUDGET_RESERVATION`
  instead of calling the oracle again.
* `ParallelEvaluationRunner` reports the batch as failed rather than dispatching
  it a second time.
* `propose_hypothesis_service` raises a conflict instead of writing a second
  hypothesis under replayed accounting.

Released capacity is reused only by a **new** operation id, never by replaying
the released one.

### Atomicity

A reservation is admitted as a whole quantity or not at all. Admission is a
single conditional `UPDATE`:

```sql
UPDATE run_budget_usage
   SET queries_reserved = queries_reserved + :quantity
 WHERE run_id = :run_id AND queries_reserved + :quantity <= :limit
```

`rowcount = 1` means the full quantity was admitted; `0` means none of it was.
The predicate includes the requested quantity, so a batch that only partially
fits the remaining capacity is refused whole instead of pushing the counter past
the limit (a five-unit limit does not admit a six-unit batch, and does not admit
a two-unit batch when only one unit remains). The database, not a Python counter
or process-local semaphore, is the admission authority, so the invariant
`admitted <= configured limit` holds across threads, workers, and service
instances. **No transaction is held open across an oracle call, a fit, or a
remote Nodus task**: reservations and outcomes use short transactions.

SQLite (the test/dev backend) additionally takes a short in-process guard to
avoid its single-writer lock errors; this is contention management only. MySQL
production relies on the conditional `UPDATE` and row locking.

---

## 3. Concurrency slots

`max_concurrent_remote_tasks` is enforced per run with durable slot reservations:

* A batch slot request is **all-or-none**: a runner never holds some slots while
  waiting for the rest, so concurrent runners cannot deadlock.
* Slots are released when the task is terminal, cancelled, or definitively never
  submitted. Duplicate completion/cancellation events do not double-release.
* Stale slots from crashed workers are reconciled after 900 seconds
  (`recover_stale_reservations`). A local reservation TTL proves only that this
  process stopped reporting; it does not prove the provider task finished. A
  slot is therefore released only when the persisted provider state shows it
  cannot belong to a live task, using the invariant that each in-flight remote
  task holds exactly one slot: `active_remote_tasks - live_remote_tasks` slots
  are provably free and are released, and every remaining stale slot is marked
  `ambiguous` and stays charged. This keeps the concurrency ceiling intact for a
  slow or disconnected worker whose task is still running at the provider.
* Stale query/evaluation/hypothesis reservations are always marked `ambiguous`
  and remain charged.
* The existing global adapter concurrency (`NODUS_MAX_CONCURRENCY`) still applies;
  the effective batch concurrency is the minimum of the global and per-run values.
* Saturation is **backpressure**, not exhaustion. A batch that cannot obtain
  slots within 5 seconds records `CONCURRENCY_LIMIT_REACHED` for those tasks
  without ending the run.

---

## 4. Runtime limits

The clock starts lazily when the first unit of work is admitted. The deadline is
persisted on `runs.deadline_at` as `started_at + max_runtime_seconds` (UTC) and is
checked before every query, evaluation, hypothesis, and remote dispatch.

* At or after the deadline, no new work is admitted; the reason is
  `TIME_LIMIT_EXCEEDED`.
* `detect_expired_runs` finds active runs whose persisted deadline has passed,
  so a restart cannot leave an expired run admitting work.
* The runtime limit stops **admission**. It cannot interrupt an already-running
  blocking call or provider work that was already submitted.

---

## 5. Compute spend: what is actually guaranteed

Cost states are kept separate and unavailable data is `null`, never `0`:

| State | Meaning |
|---|---|
| `estimated_cost_usd` | Application-side estimate before/during execution |
| `accruing_cost_usd` | Best-effort usage not yet finalized by the provider |
| `settled_cost_usd` | Cost confirmed by provider billing/settlement |
| `cost_data_state` | `not_started`, `estimated_only`, `pending_settlement`, `settled`, `unavailable` |

| Execution mode | Enforcement | Guarantee |
|---|---|---|
| `local` / `mock` | `not_applicable` | No provider charge exists to cap |
| `nodus_remote` | `application_estimate` | Stops future scheduling when the known estimate reaches the ceiling; does not cap already-running work |
| any | `provider_hard_cap` | **Not supported** by `nodus-compute 1.0.27`; never claimed |

Remote policy:

* `max_compute_spend_usd = 0` admits **no** potentially billable remote work.
* A positive ceiling denies new work once `settled + max(estimated, accruing)`
  reaches it, and records `COMPUTE_BUDGET_EXHAUSTED`.
* If cost data is unavailable, work is still admitted but the summary reports
  `null` costs and a limitation explaining that the ceiling cannot be verified.
  An estimate is never reported as settled actual spend.

---

## 6. Structured exhaustion and run lifecycle

| Stop reason | Run status | Terminal? |
|---|---|---|
| `QUERY_BUDGET_EXHAUSTED` | `budget_exhausted` | yes |
| `EVALUATION_LIMIT_REACHED` | `budget_exhausted` | yes |
| `HYPOTHESIS_LIMIT_REACHED` | `budget_exhausted` | yes |
| `COMPUTE_BUDGET_EXHAUSTED` | `budget_exhausted` | yes |
| `TIME_LIMIT_EXCEEDED` | `budget_exhausted` | yes |
| `CONCURRENCY_LIMIT_REACHED` | unchanged | no — backpressure |
| `CANCELLED` | `cancelled` | yes (researcher action) |
| `FAILED` | `failed` | yes |

When a terminal limit is reached the manager atomically records the decision
(the **first** reason wins), sets `runs.stop_reason`, transitions the run to
`budget_exhausted`, writes a `BUDGET_EXHAUSTED` audit event, and leaves all
completed observations, evaluations, hypotheses, and prior audit events intact.
Repeated or concurrent exhaustion signals are idempotent.

A resource-stopped run is **not** a successful scientific discovery. If the
scientific stopping criteria were satisfied before exhaustion, the engine
reports the criterion as the termination reason; otherwise the result is
`terminated_budget_exhausted`.

`cancel_outstanding_run_tasks` marks outstanding compute tasks cancelled, releases
their slots, records the affected provider task IDs, and states explicitly that
provider-side cancellation was not confirmed. A cancellation failure never
erases the exhaustion reason.

---

## 7. API contract

`GET /api/v1/runs/{run_id}/budget` returns `{"run_id": ..., "budget": {...}}`.
`GET /api/v1/runs/{run_id}/metrics` includes the same object under `budget`
alongside the unchanged `limits`/`usage` keys.

```json
{
  "run_id": "run-abc123",
  "budget": {
    "run_id": "run-abc123",
    "schema_version": "1.0",
    "execution_mode": "nodus_remote",
    "status": "running",
    "limits": {
      "max_oracle_queries": 5,
      "max_candidate_evaluations": 20,
      "max_concurrent_remote_tasks": 2,
      "max_runtime_seconds": 900,
      "max_generated_hypotheses": 10,
      "max_compute_spend_usd": 1.5
    },
    "usage": {
      "oracle_queries": {
        "limit": 5, "used": 2, "remaining": 3,
        "exhausted": false, "enforcement": "application", "disabled": false
      },
      "candidate_evaluations": {
        "limit": 20, "used": 4, "remaining": 16,
        "exhausted": false, "enforcement": "application", "disabled": false
      },
      "generated_hypotheses": {
        "limit": 10, "used": 6, "remaining": 4,
        "exhausted": false, "enforcement": "application", "disabled": false
      },
      "query_outcomes": {
        "attempted": 2, "completed": 2, "failed": 0, "released": 0
      },
      "evaluation_outcomes": {
        "attempted": 4, "completed": 4, "failed": 0, "released": 0
      },
      "active_remote_tasks": 0,
      "max_concurrent_remote_tasks": 2
    },
    "runtime": {
      "started_at": "2026-10-23T10:00:00+00:00",
      "deadline_at": "2026-10-23T10:15:00+00:00",
      "elapsed_seconds": 42.0,
      "remaining_seconds": 858.0
    },
    "cost": {
      "estimated_cost_usd": 0.03,
      "accruing_cost_usd": null,
      "settled_cost_usd": null,
      "data_state": "estimated_only",
      "enforcement": "application_estimate",
      "provider_hard_cap_supported": false
    },
    "is_exhausted": false,
    "stop_reason": null,
    "raw_stop_reason": null,
    "zero_semantics": { "max_oracle_queries": "Zero permits no oracle query; ..." },
    "limitations": [
      "nodus-compute 1.0.27 exposes no verified per-run spending cap. ..."
    ]
  }
}
```

Unavailable money serializes as `null`. The dashboard must not render it as `$0`
or a 100% remaining bar.

---

## 8. Code integration points

| Boundary | Integration |
|---|---|
| Oracle invocation | `DiscoveryEngine(..., budget=RunBudgetManager)` reserves before `oracle.query()` and records completed/failed after; retries reserve again |
| Candidate evaluation | `ParallelEvaluationRunner(..., budget=manager)` reserves the batch before dispatch and refuses to schedule when denied |
| Remote scheduling | Per-run slot acquisition around each adapter batch; effective concurrency is the minimum of global and per-run limits |
| Hypothesis generation | `propose_hypothesis_service` reserves one slot before persisting a new hypothesis; the engine reserves generator/scientist candidates it adopts |
| Run lifecycle | `budget_exhausted` status + `stop_reason` + `BUDGET_EXHAUSTED` audit; terminal cancellation and failure are never overwritten |
| API | Nested/legacy budget validation on create; `GET /runs/{id}/budget`; `budget` block in metrics |
| Dashboard | Run-ID budget panel showing limits vs usage, remaining capacity, concurrency, runtime, cost states, and exhaustion |

---

## 9. Persistence and recovery

Migration `e4b7c1a29d63_add_per_run_budget_manager.py` adds:

* `runs.max_concurrent_remote_tasks`, `runs.max_generated_hypotheses`,
  `runs.budget_config_json`, `runs.deadline_at`
* `run_budget_usage` — one authoritative counter row per run
* `budget_reservations` — idempotent admission ledger with
  `UNIQUE(run_id, resource, operation_id)`

The migration is reversible. Existing rows remain valid; usage rows are created
lazily with the documented defaults. `recover_stale_reservations` reconciles
crashed-worker reservations, and `detect_expired_runs` closes expired runs after
a restart. Budget state is never rolled back because later work exhausted a
limit.

---

## 10. Tests

```bash
cd backend
python -m pytest tests/test_budget_manager.py -q
python -m pytest tests/api/test_v1_budget.py -q
python -m pytest tests/test_query_controls.py tests/test_nodus_parallel.py -q
python -m pytest -q
```

`tests/test_budget_manager.py` covers configuration validation and zero
semantics, the exact five-query boundary and the denied sixth attempt,
whole-quantity batch admission at and beyond the limit boundary, reservation
replay in the `reserved`, `committed`, `released`, and `ambiguous` states,
ten-thread admission races, retry accounting, runtime boundaries, idempotent
exhaustion, preservation of completed observations, cost-state honesty, remote
slot recovery with and without a live provider task, and
hypothesis/evaluation limits. Frontend changes:
`cd frontend && npm run lint && npm run build`.

---

## 11. Known limitations

1. **No provider hard cap.** Cost control is application-side and estimate-based;
   `provider_hard_cap_supported` is always `false` for Nodus 1.0.27.
2. **No verified per-task cost metadata.** Accruing/settled values stay `null`
   until a caller supplies provider data; the ceiling cannot prevent charges
   from already-running work.
3. **Ambiguous executions stay charged.** If a dispatch may have reached the
   oracle, releasing its reservation could bypass accounting.
4. **Runtime cannot pre-empt running work.** It prevents new admission only.
5. **Provider cancellation is not confirmed** by the pinned SDK; cancelled task
   IDs are recorded without claiming confirmation.
6. **SQLite concurrency uses a short process guard** for lock contention; the
   database conditional `UPDATE` remains the cross-process authority (MySQL in
   production).
7. **Budget injection, not API launch.** `POST /runs/{id}/start` starts a queued
   run through the lifecycle manager; a persisted budget governs execution only
   when a `RunBudgetManager` is injected into `DiscoveryEngine` and
   `ParallelEvaluationRunner`. `GET /budget` lazily initializes the counter row
   for legacy runs.
8. **Stale remote slots stay charged while a provider task is live.** A worker
   that vanished without reporting cannot be distinguished from one whose
   provider task is still running, so `recover_stale_reservations` keeps those
   slots charged until the persisted task reaches a terminal state. Provider-aware
   reconciliation would tighten this further.
9. **Align engine-local and persisted limits.** The persisted budget is
   authoritative. If a `DiscoveryRunConfig.max_query_budget` is *stricter* than
   the persisted `max_oracle_queries`, the engine stops at its local limit but
   does not falsely mark the persisted budget exhausted (the remaining persisted
   capacity stays visible). Configure them consistently when both are used.
