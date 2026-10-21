# CLARITY REST API Specification

**Document Version:** 1.0.0 (Issue #19 Baseline)  
**Status:** Confirmed  
**Implementation References:**
- Route Blueprints: [backend/app/routes/runs.py](../backend/app/routes/runs.py), [backend/app/routes/health.py](../backend/app/routes/health.py)
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
      }
    }
    ```

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
