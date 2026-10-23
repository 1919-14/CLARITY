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
   - Audit trail records cannot be modified or deleted via application or REST interfaces.
   - Corrections or state changes (e.g., hypothesis unpinning, task retries, run cancellation) append new events preserving full historical provenance.

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

Run totals are derived directly from underlying task records. The service endpoint `reconcile_run_usage(session, run_id)` compares:
- Derived task count vs recorded completed tasks.
- Sum of task durations vs run wall-clock duration.
- Total oracle queries and candidate evaluations consumed across all tasks.
- Task counts broken down by status: `tasks_submitted`, `tasks_completed`, `tasks_failed`, `tasks_timed_out`, `tasks_cancelled`.

If discrepancies arise, the reconciliation report returns `is_reconciled: false` with detailed discrepancy metrics.

---

## 3. Cost Classification & Semantics

Every cost figure in CLARITY is classified into one of four distinct states:

| Cost State | Meaning | Serialization |
|---|---|---|
| `estimated` | Application-calculated using documented pricing models and measured task durations. | `amount_usd: <float>`, `state: "estimated"`, accompanied by `pricing_assumptions` and `calculation_method`. |
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
  - Missing duration: Uses `0.00 USD` base estimate with flagged incomplete status.

> **Zero Spend Semantics**: A cost of `0.0` is only returned when spend was actually measured as zero (e.g. local compute). If remote compute cost is unknown or unavailable from the provider, `amount_usd` is serialized as `null` with state `unavailable`.

---

## 4. Append-Only Audit Trail

### 4.1 Event Types

The audit trail records major lifecycle transitions:

- **Run Lifecycle**: `RUN_CREATED`, `RUN_STARTED`, `RUN_COMPLETED`, `RUN_FAILED`, `RUN_CANCELLED`, `RUN_TIMEOUT`
- **Budget Lifecycle**: `BUDGET_CONFIGURED`, `BUDGET_WARNING`, `BUDGET_EXHAUSTED`
- **Task Lifecycle**: `TASK_SUBMITTED`, `TASK_STARTED`, `TASK_COMPLETED`, `TASK_FAILED`, `TASK_TIMED_OUT`, `TASK_RETRYING`, `TASK_CANCELLED`
- **Oracle Lifecycle**: `ORACLE_QUERY_SUBMITTED`, `ORACLE_QUERY_EXECUTED`, `OBSERVATION_RECORDED`
- **Hypothesis Lifecycle**: `HYPOTHESIS_PROPOSED`, `HYPOTHESIS_EVALUATED`, `HYPOTHESIS_RANKED`, `HYPOTHESIS_PINNED`, `HYPOTHESIS_EXCLUDED`, `HYPOTHESIS_RE_EVALUATED`
- **Reporting Lifecycle**: `REPORT_GENERATED`, `REPORT_EXPORTED`

### 4.2 Event Schema

```json
{
  "event_id": "evt-7a91fc82d41b",
  "run_id": "run-001",
  "event_type": "TASK_COMPLETED",
  "actor": "system",
  "timestamp": "2026-10-23T12:00:05.123456Z",
  "metadata": {
    "task_id": "tsk-42",
    "execution_mode": "nodus",
    "provider_task_id": "nodus-job-8891",
    "duration_seconds": 12.5,
    "status": "completed"
  }
}
```

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
