# Compute-Usage Tracking and Append-Only Audit Trail (Issue #15)

CLARITY provides reliable compute-usage tracking and an append-only audit trail for discovery runs.
Researchers and operators can inspect what happened during a run, which resources were consumed, how cost figures were derived, and why the run completed, failed, or stopped.

---

## 1. Core Principles & Honesty Invariants

1. **Distinguish Measured, Estimated, and Unavailable Data**:
   - Provider-reported data (e.g. remote execution IDs, settled costs) is recorded when supplied by the execution environment.
   - Application-side cost calculations are explicitly marked as `estimated` and include documented pricing assumptions and calculation formulas.
   - Missing or unmeasurable provider data is classified as `unavailable` (`null` spend); it is **never** fabricated, guessed, or represented as $0.00 spend.

2. **Append-Only Immutability**:
   - Audit trail records cannot be modified or deleted via application or REST interfaces; the ORM/session layer rejects UPDATE/DELETE on `audit_events`.
   - Corrections or state changes (e.g., hypothesis unpinning, task retries, run cancellation) append new events preserving full historical provenance. A correction appends `AUDIT_EVENT_CORRECTED` with `corrects_event_id` pointing at the superseded event (Issue #17).

3. **Idempotency & Re-entrancy**:
   - Ingestion of duplicate task lifecycle events or audit event IDs does not duplicate records or double-count compute usage duration.

4. **Credential & Secret Redaction**:
   - Passwords, authorization tokens, bearer tokens, API keys, and connection strings are scrubbed recursively prior to storage or audit logging.

5. **Durability Across Failures**:
   - Audit records and task execution logs remain persisted and queryable even if a run fails, times out, or exhausts its budget.

---

## 2. Compute-Usage Tracking

### 2.1 Task-Level Records

Each unit of computational work (hypothesis evaluation, oracle query batch, or experiment evaluation) is tracked individually in the `compute_tasks` table:

| Field | Type | Description |
|---|---|---|
| `task_id` | string | Unique task identifier (`tsk-...`) |
| `run_id` | string | Associated CLARITY run ID |
| `hypothesis_id` | string (optional) | Hypothesis being evaluated |
| `experiment_id` | string (optional) | Candidate experiment being evaluated |
| `execution_mode` | string | `local` (sequential local) or `nodus` (remote cluster) |
| `task_status` | string | `pending`, `running`, `completed`, `failed`, `timed_out`, `cancelled`, `retrying` |
| `submitted_at` | timestamp (UTC) | When the task was enqueued |
| `started_at` | timestamp (UTC) | When execution began |
| `completed_at` | timestamp (UTC) | When execution terminated |
| `duration_seconds`| float | Measured wall-clock duration |
| `oracle_queries_consumed` | int | Oracle queries consumed by this task |
| `candidate_evaluations_consumed` | int | Candidate evaluations performed |
| `provider_task_id`| string (optional) | Remote execution task ID returned by Nodus SDK |
| `provider_metadata`| JSON (optional) | Non-sensitive provider metadata |
| `error_message` | string (optional) | Scrubbed error message if failed/timed out |

### 2.2 Run-Level Derivation & Reconciliation

Task counters, execution modes, and durations are derived directly from underlying task records using aggregate database queries. Query and evaluation totals are tracked via run budget usage records and verified during reconciliation against entity observations and candidate tasks. The service endpoint `reconcile_run_usage(session, run_id)` compares:
- Completed task count vs completed evaluation count (flagging positive evaluations without matching candidate evaluation tasks).
- Candidate evaluation tasks completed vs recorded evaluation completions.
- Sum of task durations vs run wall-clock duration (sequential duration for local execution; maximum individual task duration for remote execution).
- Recorded oracle query count from run usage vs stored observation entities in the database.
- Task counts broken down by status (`tasks_submitted`, `tasks_completed`, `tasks_failed`, `tasks_timed_out`, `tasks_cancelled`, `tasks_retried`) against total recorded tasks.

If discrepancies arise, the reconciliation report returns `is_reconciled: false` with detailed discrepancy descriptions in `discrepancies`.

---

## 3. Cost Classification & Semantics

Every cost figure in CLARITY is classified into one of four distinct states:

| Cost State | Meaning | Serialization |
|---|---|---|
| `estimated` | Application-calculated using documented pricing models and measured task durations. | `amount_usd: <float>`, `state: "estimated"`, accompanied by `pricing_assumptions`, `calculation_method`, and `is_complete`. |
| `accruing` | Provider-reported usage or charges that are still accumulating and not yet final. | `amount_usd: <float>`, `state: "accruing"`. |
| `settled` | Finalized provider-reported billing charges. | `amount_usd: <float>`, `state: "settled"`. |
| `unavailable` | Insufficient information from the provider to determine actual spend. | `amount_usd: null`, `state: "unavailable"`. |

### Application Pricing Assumptions (`estimated` state)
- **Sequential Local Execution (`local`)**:
  - Hardware: Local host CPU.
  - Rate: $0.00 / hour.
  - Formula: `0.00 USD`.
- **Nodus Remote Execution (`nodus`)**:
  - Rate: $0.05 / task minute ($3.00 / compute hour).
  - Formula: `(duration_seconds / 60.0) * 0.05 USD`.
  - Incomplete / Unknown duration: When remote tasks have missing duration, the estimate is explicitly flagged with `is_complete: false` and `completeness: "partial"`. If all remote tasks have missing duration with zero measured duration and no query/eval spend, the estimate is classified as `state: "unavailable"` (`amount_usd: null`) rather than an unqualified $0.00 estimate.

> **Zero Spend Semantics**: A cost of `0.0` is only returned when spend was actually measured as zero (e.g. local compute). If remote compute cost is unknown or unavailable from the provider, `amount_usd` is serialized as `null` with state `unavailable`.

---

## 4. Append-Only Audit Trail

### 4.1 Event Types

The audit trail records major lifecycle transitions:

- **Run Lifecycle**: `RUN_CREATED`, `RUN_STARTED`, `RUN_COMPLETED`, `RUN_FAILED`, `RUN_CANCELLED`, `RUN_TIMEOUT`
- **Budget Lifecycle**: `BUDGET_CONFIGURED`, `BUDGET_WARNING`, `BUDGET_EXHAUSTED`
- **Task Lifecycle**: `TASK_SUBMITTED`, `TASK_STARTED`, `TASK_COMPLETED`, `TASK_FAILED`, `TASK_TIMED_OUT`, `TASK_RETRYING`, `TASK_CANCELLED`
- **Oracle Lifecycle**: `ORACLE_QUERY_SUBMITTED`, `ORACLE_QUERY_EXECUTED`, `OBSERVATION_RECORDED`
- **Hypothesis Lifecycle**: `HYPOTHESIS_PROPOSED`, `HYPOTHESIS_REVISED`, `HYPOTHESIS_EVALUATED`, `HYPOTHESIS_RANKED`, `HYPOTHESIS_PINNED`, `HYPOTHESIS_UNPINNED`, `HYPOTHESIS_EXCLUDED`, `HYPOTHESIS_RESTORED`, `HYPOTHESIS_REEVALUATED`, `HYPOTHESIS_REEVALUATE_REQUESTED`, `HYPOTHESIS_REEVALUATION_CONSUMED`
- **History Integrity**: `AUDIT_EVENT_CORRECTED`
- **Reporting Lifecycle**: `REPORT_GENERATED`, `REPORT_EXPORTED`

### 4.2 Event Schema

```json
{
  "event_id": "evt-7a91fc82d41b",
  "run_id": "run-001",
  "event_type": "TASK_COMPLETED",
  "actor": "system",
  "actor_id": null,
  "hypothesis_id": null,
  "revision_id": null,
  "correlation_id": null,
  "before": null,
  "after": null,
  "reason": null,
  "corrects_event_id": null,
  "timestamp": "2026-10-23T12:00:05.123456+00:00",
  "metadata": {
    "task_id": "tsk-42",
    "execution_mode": "nodus",
    "provider_task_id": "nodus-job-8891",
    "duration_seconds": 12.5,
    "status": "completed"
  }
}
```

Issue #17 adds the structured provenance fields (`actor_id`, `hypothesis_id`,
`revision_id`, `correlation_id`, `before`, `after`, `reason`, `corrects_event_id`)
and the `GET /api/v1/runs/{run_id}/history` read model (filtering, cursor
pagination). See [researcher-history.md](researcher-history.md) for the full
event, revision and evaluation-provenance contract.

### 4.3 Data Retention & Chronology

- **Ordering**: Events are indexed and queried in ascending chronological order (`timestamp ASC, event_id ASC`).
- **Pagination**: Standard limit/offset pagination with `count`, `limit`, and `offset` metadata.
- **Filtering**: Query parameter `event_type` enables targeted filtering (e.g. `?event_type=BUDGET_EXHAUSTED`).
- **Retention**: Audit events are retained indefinitely. Deletion is prohibited by database foreign key constraints (`ON DELETE RESTRICT`) and application service layer enforcement.

---

## 5. Known Provider Limitations (`nodus-compute`)

1. **No Real-Time Settled Cost Callback**:
   - `nodus-compute` does not stream finalized financial charges during execution.
   - Run totals during active execution rely on application-side estimates derived from measured duration.
2. **Provider Execution IDs**:
   - In offline mock environments or when remote connectivity fails prior to job submission, `provider_task_id` may be null. CLARITY preserves the internal `task_id` for auditing.
3. **Pre-Execution Cost Guarantees**:
   - Nodus does not guarantee static pre-execution cost ceilings. The CLARITY budget manager enforces application-side spend caps as admission gates.

---

## 6. REST API Endpoints

### 6.1 Get Run Usage Summary
```http
GET /api/v1/runs/{run_id}/usage
```
Response:
```json
{
  "run_id": "run-001",
  "execution_mode": "nodus",
  "tasks": {
    "submitted": 10,
    "completed": 9,
    "failed": 1,
    "timed_out": 0,
    "cancelled": 0
  },
  "consumption": {
    "oracle_queries_consumed": 5,
    "candidate_evaluations_consumed": 9,
    "total_duration_seconds": 142.5
  },
  "cost": {
    "state": "estimated",
    "estimated_cost_usd": 0.1187,
    "accruing_cost_usd": null,
    "settled_cost_usd": null,
    "calculation_method": "nodus_remote_duration_rate",
    "pricing_assumptions": {
      "rate_per_minute_usd": 0.05,
      "currency": "USD"
    }
  },
  "reconciliation": {
    "is_reconciled": true,
    "discrepancies": []
  }
}
```

### 6.2 List Run Tasks
```http
GET /api/v1/runs/{run_id}/tasks?limit=50&offset=0
```

### 6.3 Query Run Audit Events
```http
GET /api/v1/runs/{run_id}/audit-events?event_type=TASK_FAILED&limit=100&offset=0
```

### 6.4 Append Audit Event
```http
POST /api/v1/runs/{run_id}/audit-events
Content-Type: application/json

{
  "event_type": "REPORT_GENERATED",
  "actor": "researcher",
  "metadata": {
    "format": "json",
    "destination": "download"
  }
}
```
