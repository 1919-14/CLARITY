# CLARITY REST API Specification

**Document Version:** 1.1.0 (Issue #19 & Issue #20 Baseline)  
**Status:** Confirmed  
**Implementation References:**
- Unversioned Blueprints: [backend/app/routes/runs.py](../backend/app/routes/runs.py), [backend/app/routes/health.py](../backend/app/routes/health.py)
- Versioned v1 Blueprints: [backend/app/routes/v1/](../backend/app/routes/v1/)
- OpenAPI Specification: [docs/openapi_v1.yaml](openapi_v1.yaml)
- Application Factory: [backend/app/__init__.py](../backend/app/__init__.py)
- Persistence Adapter: [backend/app/adapters/persistence/](../backend/app/adapters/persistence/)

---

## 1. Overview and Design Principles

1. **Decoupled Architecture**: Routes interact exclusively with the persistence layer (`ClarityRepository`) and domain services. Domain algorithms and mathematical logic remain outside route handlers.
2. **Explicit Allowlist Serialization**: All responses serialize through strict allowlists. Internal secret keys, hidden ground-truth formulas, and evaluator secrets are never exposed in any API response.
3. **Structured Error Payloads**: All errors (4xx, 5xx) return consistent JSON objects:
   ```json
   {
     "error": "Bad Request",
     "message": "Field 'name' is required and must be a non-empty string.",
     "status_code": 400
   }
   ```
4. **Non-Destructive Actions**: Researcher interactions (pinning, excluding) update state flags and emit immutable `AuditEventModel` records without modifying historical evaluations.

---

## 2. Endpoints Specification

### 2.1 System & Health
* **`GET /api/health`**
  - Response (200 OK):
    ```json
    { "status": "healthy", "service": "clarity-backend" }
    ```

---

### 2.2 Discovery Runs

* **`POST /api/runs`**
  - Creates a new discovery run with configured budget and domain constraints.
  - Request Body:
    | Field | Type | Required | Description |
    |---|---|---|---|
    | `name` | string | Yes | Human-readable run name |
    | `domain` | string | No | Problem domain (default: `"default"`) |
    | `execution_mode` | string | No | `"local"`, `"nodus_remote"`, or `"mock"` (default: `"local"`) |
    | `sampling_strategy` | string | No | Active sampling strategy (default: `"active_disagreement"`) |
    | `max_queries` | int | No | Maximum allowed oracle queries (> 0) |
    | `max_evaluations` | int | No | Maximum candidate evaluations (> 0) |
    | `max_runtime_seconds` | int | No | Timeout limit in seconds (> 0) |
    | `spend_ceiling_usd` | float | No | Spend limit in USD (>= 0.0) |
    | `run_id` | string | No | Optional custom UUID run identifier |
  - Response (201 Created):
    ```json
    {
      "run": {
        "run_id": "run-a1b2c3d4e5f6",
        "name": "Polynomial Active Discovery",
        "domain": "1d_polynomial",
        "status": "pending",
        "execution_mode": "local",
        "sampling_strategy": "active_disagreement",
        "stop_reason": null,
        "max_queries": 20,
        "max_evaluations": 60,
        "max_runtime_seconds": 180,
        "spend_ceiling_usd": 10.0,
        "created_at": "2026-10-21T16:00:00+00:00",
        "started_at": null,
        "completed_at": null
      }
    }
    ```

* **`GET /api/runs`**
  - Lists runs with pagination.
  - Query Parameters: `limit` (int, default 50, max 100), `offset` (int, default 0).
  - Response (200 OK):
    ```json
    {
      "runs": [...],
      "count": 10,
      "limit": 50,
      "offset": 0
    }
    ```

* **`GET /api/runs/{run_id}`**
  - Retrieves a specific discovery run.
  - Response (200 OK):
    ```json
    { "run": { ... } }
    ```
  - Error: 404 Not Found if run does not exist.

* **`POST /api/runs/{run_id}/cancel`**
  - Cancels an active or pending discovery run.
  - Request Body (optional): `{"reason": "Manual cancellation"}`
  - Response (200 OK): Updated run with `status: "cancelled"`.
  - Error: 409 Conflict if run is already in terminal state (`completed`, `cancelled`, `failed`, `budget_exhausted`).

---

### 2.3 Hypotheses & Provenance

* **`GET /api/runs/{run_id}/hypotheses`**
  - Lists candidate hypotheses for a run with full revision history and latest evaluation.
  - Query Parameters: `include_excluded` (bool, default `true`).
  - Response (200 OK):
    ```json
    {
      "hypotheses": [
        {
          "hypothesis_id": "hyp-01",
          "run_id": "run-01",
          "model_family": "1d_polynomial",
          "current_revision": 1,
          "is_pinned": false,
          "is_excluded": false,
          "created_at": "2026-10-21T16:00:00+00:00",
          "updated_at": "2026-10-21T16:00:00+00:00",
          "latest_revision": {
            "revision_id": "rev-01",
            "revision_number": 1,
            "formula": "y = 0.5x^2 - 1.0",
            "parameters": {"c2": 0.5, "c0": -1.0},
            "complexity": 2,
            "creation_source": "generator",
            "created_at": "2026-10-21T16:00:00+00:00"
          },
          "latest_evaluation": {
            "evaluation_id": "eval-01",
            "status": "completed",
            "sample_count": 10,
            "dataset_version": 10,
            "metrics": {"mae": 0.045, "rmse": 0.052},
            "held_out_metrics": {"mae": 0.081, "rmse": 0.093},
            "evaluated_at": "2026-10-21T16:05:00+00:00"
          },
          "revisions": [...]
        }
      ],
      "count": 1
    }
    ```

* **`POST /api/runs/{run_id}/hypotheses`**
  - Proposes a new researcher hypothesis and logs an immutable audit event (`HYPOTHESIS_PROPOSED`).
  - Request Body:
    | Field | Type | Required | Description |
    |---|---|---|---|
    | `model_family` | string | Yes | Model family identifier |
    | `formula` | string | Yes | Mathematical/symbolic formula expression |
    | `parameters` | object | No | Dictionary of coefficients/parameters |
    | `complexity` | int | No | Complexity proxy (>= 1, default: 1) |
  - Response (201 Created): Created hypothesis object.

* **`PATCH /api/runs/{run_id}/hypotheses/{hypothesis_id}`**
  - Updates researcher preference flags non-destructively and logs audit events (`HYPOTHESIS_PINNED`, `HYPOTHESIS_UNPINNED`, `HYPOTHESIS_EXCLUDED`, `HYPOTHESIS_RESTORED`).
  - Request Body:
    ```json
    { "is_pinned": true, "is_excluded": false }
    ```
  - Response (200 OK): Updated hypothesis object.

---

### 2.4 Observations

* **`GET /api/runs/{run_id}/observations`**
  - Retrieves observations query history for a run.
  - Query Parameters: `only_training` (bool, default `false`).
  - Response (200 OK):
    ```json
    {
      "observations": [
        {
          "observation_id": "obs-01",
          "run_id": "run-01",
          "input_id": "inp-01",
          "input_values": {"x": 2.5},
          "output_values": {"y": 5.25},
          "status": "success",
          "sequence_number": 1,
          "is_held_out": false,
          "error_code": null,
          "error_message": null,
          "noise_metadata": null,
          "created_at": "2026-10-21T16:01:00+00:00"
        }
      ],
      "count": 1
    }
    ```

---

### 2.5 Metrics & Budget

* **`GET /api/runs/{run_id}/metrics`**
  - Retrieves execution progress, budget ceiling limits, and distinct cost categories (estimated, accruing, settled).
  - Response (200 OK):
    ```json
    {
      "run_id": "run-01",
      "status": "running",
      "stop_reason": null,
      "limits": {
        "max_queries": 20,
        "max_evaluations": 60,
        "max_runtime_seconds": 180,
        "spend_ceiling_usd": 10.0
      },
      "usage": {
        "queries_executed": 12,
        "evaluations_scheduled": 30,
        "evaluations_completed": 28,
        "estimated_cost_usd": 0.084,
        "live_accruing_cost_usd": 0.079,
        "settled_cost_usd": null,
        "is_budget_exhausted": false,
        "exhaustion_reason": null,
        "recorded_at": "2026-10-21T16:10:00+00:00"
      },
      "budget": {
        "schema_version": "1.0",
        "limits": {"max_oracle_queries": 20, "max_candidate_evaluations": 60},
        "usage": {"oracle_queries": {"used": 12, "remaining": 8}},
        "is_exhausted": false,
        "stop_reason": null
      }
    }
    ```
  - In v1, `budget` is the complete Issue #14 summary. See [budget-manager.md](budget-manager.md) for the full schema; unavailable monetary values are `null`, never `0`.

---

### 2.6 Scientific Reports & Benchmarks

* **`GET /api/runs/{run_id}/report`**
  - Retrieves the generated final scientific report.
  - Response (200 OK - Pending):
    ```json
    {
      "run_id": "run-01",
      "report": null,
      "status": "pending",
      "message": "Report has not yet been generated for this run."
    }
    ```
  - Response (200 OK - Generated):
    ```json
    {
      "report": {
        "report_id": "rep-01",
        "run_id": "run-01",
        "status": "generated",
        "ranked_candidates": [
          { "hypothesis_id": "hyp-01", "rank": 1, "mae": 0.012 }
        ],
        "stop_reason": "target_accuracy_reached",
        "limitations": "Evaluated within [-10, 10] domain.",
        "artifact_uri": "reports/run-01-summary.json",
        "generated_at": "2026-10-21T16:15:00+00:00"
      }
    }
    ```

* **`GET /api/runs/{run_id}/benchmarks`**
  - Retrieves ground-truth benchmark metrics (Hit@K, MRR, error metrics).
  - Response (200 OK):
    ```json
    {
      "benchmarks": [
        {
          "benchmark_id": "bench-01",
          "run_id": "run-01",
          "benchmark_name": "polynomial_cubic_benchmark",
          "target_label": "target_cubic",
          "hit_at_1": true,
          "hit_at_3": true,
          "hit_at_5": true,
          "reciprocal_rank": 1.0,
          "best_candidate_id": "hyp-target",
          "queries_to_target": 4,
          "held_out_mae": 0.012,
          "held_out_rmse": 0.018,
          "baseline_type": "random_sampling",
          "efficiency_gain": "3.2x",
          "executed_at": "2026-10-21T16:15:00+00:00"
        }
      ],
      "count": 1
    }
    ```

---

## 3. Versioned REST API v1 (`/api/v1`)

The v1 API expands and stabilizes the endpoint surface for React frontend and external integrations under the `/api/v1` namespace.

An OpenAPI 3.0.3 machine-readable schema is provided in [docs/openapi_v1.yaml](openapi_v1.yaml).

### 3.1 Key Guarantees & Differences

1. **Version Isolation**: All endpoints are prefixed with `/api/v1` and registered via modular blueprints (`backend/app/routes/v1/`).
2. **Explicit Allowlist Serialization**: Serializers in `backend/app/routes/v1/shared.py` define strict output fields. Hidden ground truth formulas, private oracle states, evaluator internal weights, and database infrastructure strings are completely filtered out.
3. **Database Health Probe**: `GET /api/v1/health` executes a `SELECT 1` probe against the database. If unreachable, it returns HTTP 503 (`status: "degraded"`) without leaking internal error tracebacks or connection strings.
4. **Distinction of Measured Zero vs Unavailable**: Usage metrics strictly return `null` (`None`) when data is unavailable and `0` when measured as zero. Settled costs are `null` until confirmed by billing.
5. **Evaluation Pipeline Re-evaluation**: `POST /api/v1/hypotheses/{id}/reevaluate` routes re-evaluation through CLARITY's domain `EvaluationPipeline` against accumulated run observations, returning HTTP 200 with completed/failed evaluation metrics. When an asynchronous worker queue/dispatcher is configured in `EVALUATION_DISPATCHER`, work is dispatched to the background worker returning HTTP 202 Accepted.
6. **Bounded Database Pagination**: Collection listing routes (`runs`, `observations`) enforce strict limit (1–200, default 50) and offset constraints executed directly via database SQL queries (`LIMIT`, `OFFSET`, `SELECT COUNT(*)`), preventing unbounded in-memory collection slicing.

### 3.2 Endpoint Summary Table

| Method | Endpoint | Description | Status Codes |
|---|---|---|---|
| `GET` | `/api/v1/health` | Health check with database connectivity check | 200, 503 |
| `POST` | `/api/v1/runs` | Create a new discovery run | 201, 400, 409, 500 |
| `GET` | `/api/v1/runs` | List runs (pagination: `limit`, `offset`, `status`) | 200, 400, 500 |
| `GET` | `/api/v1/runs/{run_id}` | Retrieve run metadata and lifecycle state | 200, 404, 500 |
| `POST` | `/api/v1/runs/{run_id}/cancel` | Cancel an active or pending run | 200, 404, 409, 500 |
| `GET` | `/api/v1/runs/{run_id}/metrics` | Usage counters, budget limits, tiered costs, and the full budget summary | 200, 404, 500 |
| `GET` | `/api/v1/runs/{run_id}/budget` | Per-run budget: limits, remaining capacity, runtime, costs, exhaustion | 200, 404, 500 |
| `GET` | `/api/v1/runs/{run_id}/report` | Final scientific summary report (or pending state) | 200, 404, 500 |
| `GET` | `/api/v1/runs/{run_id}/benchmarks` | Ground-truth benchmark evaluations | 200, 404, 500 |
| `GET` | `/api/v1/runs/{run_id}/observations` | List observations (`only_training`, `limit`, `offset`) | 200, 400, 404, 500 |
| `GET` | `/api/v1/runs/{run_id}/hypotheses` | List candidate hypotheses (`include_excluded`) | 200, 404, 500 |
| `POST` | `/api/v1/runs/{run_id}/hypotheses` | Submit researcher-proposed hypothesis | 201, 400, 404, 409, 500 |
| `GET` | `/api/v1/hypotheses/{hypothesis_id}` | Retrieve hypothesis with full revisions and latest evaluation | 200, 404, 500 |
| `PATCH` | `/api/v1/hypotheses/{hypothesis_id}` | Update flags (`is_pinned`, `is_excluded`) non-destructively | 200, 400, 404, 500 |
| `POST` | `/api/v1/hypotheses/{hypothesis_id}/reevaluate` | Execute re-evaluation via EvaluationPipeline or enqueue (returns 200 or 202) | 200, 202, 404, 422, 500 |
| `GET` | `/api/v1/hypotheses/{hypothesis_id}/evaluations` | Historical evaluation runs for current revision | 200, 404, 500 |
| `GET` | `/api/v1/observations/{observation_id}` | Single observation details (no ground truth leakage) | 200, 404, 500 |

### 3.3 New Resource Endpoints in v1

#### 3.3.1 Resource-Level Hypotheses

* **`GET /api/v1/hypotheses/{hypothesis_id}`**
  - Response (200 OK):
    ```json
    {
      "hypothesis": {
        "hypothesis_id": "hyp-01",
        "run_id": "run-01",
        "model_family": "1d_polynomial",
        "current_revision": 1,
        "is_pinned": false,
        "is_excluded": false,
        "created_at": "2026-10-21T16:00:00+00:00",
        "updated_at": "2026-10-21T16:00:00+00:00",
        "latest_revision": {
          "revision_id": "rev-01",
          "revision_number": 1,
          "formula": "y = 2.0 * x",
          "parameters": {"slope": 2.0},
          "complexity": 1,
          "creation_source": "researcher",
          "created_at": "2026-10-21T16:00:00+00:00"
        },
        "latest_evaluation": {
          "evaluation_id": "eva-01",
          "status": "completed",
          "sample_count": 10,
          "dataset_version": 10,
          "metrics": {"mae": 0.05, "rmse": 0.06},
          "held_out_metrics": null,
          "error_message": null,
          "evaluated_at": "2026-10-21T16:02:00+00:00"
        },
        "revisions": [...]
      }
    }
    ```

* **`PATCH /api/v1/hypotheses/{hypothesis_id}`**
  - Request Body:
    ```json
    { "is_pinned": true }
    ```
  - Response (200 OK): Updated hypothesis object with new flag state. Emits audit events (`HYPOTHESIS_PINNED`, `HYPOTHESIS_UNPINNED`, `HYPOTHESIS_EXCLUDED`, or `HYPOTHESIS_RESTORED`).

* **`POST /api/v1/hypotheses/{hypothesis_id}/reevaluate`**
  - Routes re-evaluation through the domain `EvaluationPipeline` against the run's observations, returning HTTP 200 with completed/failed results. If an async task queue dispatcher is configured (`EVALUATION_DISPATCHER`), enqueues work and returns HTTP 202 Accepted.
  - Response (200 OK — Pipeline Evaluation):
    ```json
    {
      "evaluation": {
        "evaluation_id": "eva-7a3b4c5d6e7f",
        "status": "completed",
        "sample_count": 25,
        "dataset_version": 25,
        "metrics": {
          "mae": 0.042,
          "rmse": 0.061
        },
        "held_out_metrics": {
          "mae": 0.089,
          "rmse": 0.112
        },
        "error_message": null,
        "evaluated_at": "2026-10-22T22:30:00.000000+00:00"
      },
      "message": "Re-evaluation completed via evaluation pipeline."
    }
    ```
  - Response (202 Accepted — Async Dispatched):
    ```json
    {
      "evaluation": {
        "evaluation_id": "eva-pending-01",
        "status": "pending",
        "sample_count": 0,
        "dataset_version": 0,
        "metrics": {},
        "held_out_metrics": null,
        "error_message": null,
        "evaluated_at": null
      },
      "message": "Re-evaluation enqueued. Poll the evaluation history for results."
    }
    ```

* **`GET /api/v1/hypotheses/{hypothesis_id}/evaluations`**
  - Returns evaluation history for the latest revision sorted newest first.
  - Response (200 OK):
    ```json
    {
      "hypothesis_id": "hyp-01",
      "revision_id": "rev-01",
      "evaluations": [
        {
          "evaluation_id": "eva-02",
          "status": "completed",
          "sample_count": 25,
          "dataset_version": 25,
          "metrics": {"mae": 0.012, "rmse": 0.015},
          "held_out_metrics": {"mae": 0.02, "rmse": 0.024},
          "error_message": null,
          "evaluated_at": "2026-10-21T16:20:00+00:00"
        }
      ],
      "count": 1
    }
    ```

#### 3.3.2 Resource-Level Observations

* **`GET /api/v1/observations/{observation_id}`**
  - Response (200 OK):
    ```json
    {
      "observation": {
        "observation_id": "obs-01",
        "run_id": "run-01",
        "input_id": "inp-01",
        "input_values": {"x": 3.0},
        "output_values": {"y": 6.0},
        "status": "success",
        "sequence_number": 1,
        "is_held_out": false,
        "error_code": null,
        "error_message": null,
        "noise_metadata": null,
        "created_at": "2026-10-21T16:01:00+00:00"
      }
    }
    ```

### 3.4 Per-Run Budget Contract (`GET /api/v1/runs/{run_id}/budget`)

Every run has an explicit, validated budget (Issue #14). `POST /api/v1/runs`
accepts either a canonical nested `budget` object or the legacy flat limit
fields; supplying both is rejected as conflicting. Zero always means "no work of
this kind", never unlimited; an omitted optional limit uses its documented
default, and `null` optionally disables the hypothesis or spend limit.

Request (create):

```json
{
  "name": "Budgeted run",
  "execution_mode": "nodus_remote",
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

Response (200 OK) — abbreviated; the full field list is documented in
[budget-manager.md](budget-manager.md):

```json
{
  "run_id": "run-01",
  "budget": {
    "run_id": "run-01",
    "schema_version": "1.0",
    "execution_mode": "nodus_remote",
    "status": "running",
    "limits": {
      "max_oracle_queries": 25,
      "max_candidate_evaluations": 300,
      "max_concurrent_remote_tasks": 2,
      "max_runtime_seconds": 900,
      "max_generated_hypotheses": 40,
      "max_compute_spend_usd": 1.5
    },
    "usage": {
      "oracle_queries": {
        "limit": 25, "used": 3, "remaining": 22,
        "exhausted": false, "enforcement": "application", "disabled": false
      },
      "candidate_evaluations": {
        "limit": 300, "used": 12, "remaining": 288,
        "exhausted": false, "enforcement": "application", "disabled": false
      },
      "generated_hypotheses": {
        "limit": 40, "used": 8, "remaining": 32,
        "exhausted": false, "enforcement": "application", "disabled": false
      },
      "query_outcomes": {"attempted": 3, "completed": 3, "failed": 0, "released": 0},
      "evaluation_outcomes": {"attempted": 12, "completed": 12, "failed": 0, "released": 0},
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
    "zero_semantics": {"max_oracle_queries": "Zero permits no oracle query; ..."},
    "limitations": ["nodus-compute 1.0.27 exposes no verified per-run spending cap. ..."]
  }
}
```

Error behaviour:

* Invalid or conflicting budget on create → `400` with a `details` array of
  `{field, code, message}` entries.
* Unknown run → `404`.
* Exhausted hypothesis generation on `POST /runs/{id}/hypotheses` → `409` with
  `details.reason = "HYPOTHESIS_LIMIT_REACHED"`; the run becomes
  `budget_exhausted` and completed results are preserved.

Stop reasons: `QUERY_BUDGET_EXHAUSTED`, `EVALUATION_LIMIT_REACHED`,
`HYPOTHESIS_LIMIT_REACHED`, `COMPUTE_BUDGET_EXHAUSTED`, `TIME_LIMIT_EXCEEDED`,
`CONCURRENCY_LIMIT_REACHED` (backpressure only), `CANCELLED`, `FAILED`.
Terminal resource reasons transition the run to `budget_exhausted`; a
resource-stopped run is not reported as a scientific discovery.
