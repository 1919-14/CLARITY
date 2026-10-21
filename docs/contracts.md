# CLARITY Scientific Data Contracts & Boundaries

**Document Version:** 1.2.0 (Issue #4 baseline + Issue #5 hypotheses + Issue #6 fitting/evaluation)  
**Status:** Confirmed baseline; hypothesis representation and candidate generation added under Issue #5  
**Code Reference:** [backend/app/domain/contracts.py](../backend/app/domain/contracts.py)

---

## 1. Overview and Trust Boundaries

CLARITY operates under strict structural isolation to prevent scientific falsification and ground-truth leakage:

```
┌─────────────────────────────────────────────────────────────────┐
│                    ISOLATED BENCHMARK EVALUATOR                 │
│  - Holds GroundTruthRule (domain, formula, target_parameters)   │
│  - Calculates Hit@K, MRR, true held-out MAE/RMSE               │
│  - Outputs BenchmarkReport (never fed back to discovery engine) │
└───────────────────────────────┬─────────────────────────────────┘
                                │ (Strict Boundary Isolation)
                                ▼
┌───────────────────────┐               ┌─────────────────────────┐
│     ORACLE ADAPTER    │               │     DISCOVERY ENGINE    │
│  - Implements         │ ◄── Query ─── │  - Hypothesis generator │
│    OracleInterface    │ ─── Obs ────► │  - Active selector      │
│  - Pure black-box     │               │  - Local fitters        │
└───────────────────────┘               └─────────────────────────┘
```

1. **Oracle:** Accepts an `ExperimentInput` and returns an `Observation`. All parameters and secret generating equations are kept strictly private inside the adapter.
2. **Discovery Engine:** Only receives observable `Observation` objects and non-sensitive metadata.
3. **Evaluator:** Operates independently of the discovery loop. It evaluates generated candidates against hidden targets to produce a `BenchmarkReport`.
4. **Leakage Enforcement:** Automated recursive verification (`verify_no_ground_truth_leakage()`) rejects any dictionary containing forbidden prefixes (`ground_truth`, `secret_`, `true_`, `evaluator_only`, `oracle_internal`).

---

## 2. Core Schemas & Contracts

### 2.1 Experiment Input Contract (`ExperimentInput`)
Specifies an experiment proposal to submit to an oracle:
```json
{
  "input_id": "inp-a1b2c3d4e5",
  "domain": "1d_polynomial",
  "values": {
    "x": 2.5
  },
  "metadata": {
    "strategy": "active_disagreement",
    "disagreement_score": 12.4
  }
}
```
* **Validation Rules:**
  * `input_id`: Non-empty string.
  * `values`: Non-empty dictionary where values must be primitive types (`int`, `float`, `str`, `bool`).
  * `metadata`: Optional dictionary for non-sensitive planning context.

### 2.2 Observation Contract (`Observation`)
The observable record returned by an oracle query:
```json
{
  "observation_id": "obs-9f8e7d6c5b",
  "input_id": "inp-a1b2c3d4e5",
  "input_values": {
    "x": 2.5
  },
  "output_values": {
    "y": 9.5625
  },
  "status": "success",
  "sequence_number": 1,
  "timestamp": "2026-10-20T03:00:15.123456+00:00",
  "is_held_out": false,
  "noise_metadata": null,
  "error": null
}
```
* **Error Observation Example (Structured Error):**
```json
{
  "observation_id": "obs-err-01",
  "input_id": "inp-invalid-01",
  "input_values": {
    "x": 50.0
  },
  "output_values": null,
  "status": "invalid_input",
  "sequence_number": 1,
  "timestamp": "2026-10-20T03:00:16.000000+00:00",
  "is_held_out": false,
  "error": {
    "code": "OUT_OF_BOUNDS",
    "message": "Value x=50.0 is outside valid domain range [-10.0, 10.0].",
    "details": {
      "x": 50.0,
      "min_x": -10.0,
      "max_x": 10.0
    }
  }
}
```

### 2.3 Hypothesis Representation (`HypothesisRepresentation`)
Describes a candidate explanation. Issue #4 defined the base fields; Issue #5
adds lifecycle, provenance, and optional downstream metadata (full field list and
semantics in section 4.1):

```json
{
  "hypothesis_id": "hyp-1f3c9a2b7d",
  "model_family": "polynomial",
  "formula": "y = 1.0 + 2.0*x + 3.0*x^2",
  "parameters": {
    "family": "polynomial",
    "degree": 2,
    "coefficient_order": "ascending",
    "coefficients": {"c0": 1.0, "c1": 2.0, "c2": 3.0},
    "input_variable": "x",
    "output_variable": "y"
  },
  "complexity": 3,
  "revision": 1,
  "created_at": "2026-10-20T03:00:00.000000+00:00",
  "status": "candidate",
  "provenance": {
    "origin": "system_generated",
    "generator": "polynomial_candidate_generator",
    "generator_version": "1.0.0"
  }
}
```

`parameters` is the machine-readable model specification. `model_family` is a
registered family identifier, not the oracle domain label. Values in this example
are illustrative.

### 2.4 Evaluation Request & Result (`EvaluationRequest`, `EvaluationResult`)
* **Evaluation Request:**
  * `hypothesis`: `HypothesisRepresentation`
  * `observation_ids`: List of training observation IDs.
  * `held_out_observation_ids`: List of held-out observation IDs.
  * **Data Leakage Invariant:** An assertion enforces that $\text{observation\_ids} \cap \text{held\_out\_observation\_ids} = \emptyset$. Any overlap raises an immediate `ValueError`.
* **Evaluation Result:** computed by the fitting/evaluation pipeline (Issue #6). Issue #5 defined the base fields; Issue #6 adds fitting status, counts, metric metadata, dataset identifiers, warnings, and the fitted specification. The full schema is in [section 5.6](#56-standardized-result-schema); the abbreviated shape is:
```json
{
  "evaluation_id": "eval-001",
  "hypothesis_id": "hyp-003",
  "status": "completed",
  "metrics": {
    "mae": 0.042,
    "rmse": 0.051
  },
  "sample_count": 4,
  "held_out_metrics": {
    "mae": 0.085,
    "rmse": 0.096
  },
  "held_out_sample_count": 2,
  "model_family": "polynomial",
  "fitting_status": "success",
  "fitting_method": "least_squares",
  "evaluated_at": "2026-10-20T03:00:20.000000+00:00"
}
```

Values are illustrative, not measured results.

### 2.5 Benchmark Report (`BenchmarkReport`)
Calculated by `BenchmarkEvaluator` from reference ground truth:
```json
{
  "benchmark_id": "bench-001",
  "run_id": "run-001",
  "target_label": "target_degree_3_polynomial",
  "hit_at_1": false,
  "hit_at_3": true,
  "hit_at_5": true,
  "reciprocal_rank": 0.5,
  "best_candidate_id": "hyp-linear",
  "queries_to_target": 5,
  "held_out_mae": 0.015,
  "held_out_rmse": 0.022
}
```

---

## 3. Local Deterministic Simulator (`PolynomialLocalOracle`)

Available in [backend/app/adapters/oracle/local_simulator.py](../backend/app/adapters/oracle/local_simulator.py):
* **Class:** `PolynomialLocalOracle(OracleInterface)`
* **Function:** $y = 0.5x^3 - 1.0x^2 - 2.0x + 3.0$
* **Valid Domain:** $x \in [-10.0, 10.0]$
* **Structured Validation Errors:**
  * `MISSING_PARAMETER`: Missing key `'x'`.
  * `INVALID_PARAMETER_TYPE`: Parameter `'x'` is not a real number.
  * `OUT_OF_BOUNDS`: Value outside $[-10.0, 10.0]$.

---

## 4. Hypothesis Model Families & Candidate Generation (Issue #5)

**Code references:**
- [backend/app/domain/contracts.py](../backend/app/domain/contracts.py) - `HypothesisStatus`, `HypothesisOrigin`, `HypothesisProvenance`, `HypothesisRepresentation`
- [backend/app/domain/hypothesis_models.py](../backend/app/domain/hypothesis_models.py) - model family, validation, prediction, registry
- [backend/app/domain/candidate_generator.py](../backend/app/domain/candidate_generator.py) - candidate generation
- [backend/tests/test_hypotheses.py](../backend/tests/test_hypotheses.py) - focused tests

### 4.1 Representation and lifecycle

`HypothesisRepresentation` is the single candidate contract. Issue #5 extends the
Issue #4 type with optional fields; existing fields keep their meaning.

| Field | Required | Meaning |
|---|---|---|
| `hypothesis_id` | yes (generated) | Stable `hyp-...` identifier. Unchanged by evaluation or ranking. |
| `model_family` | yes | Registered family identifier, e.g. `polynomial`. Not the oracle domain label (`1d_polynomial`). |
| `formula` | yes | Human-readable summary derived from the specification. |
| `parameters` | yes | Machine-readable specification in the family's canonical shape (4.2). |
| `complexity` | yes (default 1) | Family-defined complexity; `degree + 1` for polynomials. |
| `revision` | yes (default 1) | Increments only when the specification changes, not on evaluation. |
| `created_at` | yes | UTC ISO-8601 creation timestamp. |
| `status` | yes (default `candidate`) | `candidate` = proposed, not evaluated. `evaluated` is set only when a downstream service materializes a fitted hypothesis (#6/#7); the Issue #6 pipeline reports results without mutating candidates, and the generator never sets it. |
| `provenance` | optional | `HypothesisProvenance`: origin, generator, generator version, parent, note. |
| `generation_metadata` | optional | Generator name/version, seed, configuration, initialization policy, sequence index. |
| `fitting_metadata` | optional | Reserved for downstream fitting (#6). Absent until computed. |
| `uncertainty_metadata` | optional | Reserved for downstream uncertainty work. Absent until computed. |

Rules:
- Missing optional metrics mean "not computed"; they are never serialized as zero error, perfect confidence, or a completed evaluation.
- `parameters` and the metadata payloads are stored as immutable snapshots: mappings are read-only views and sequences become tuples. Mutating the caller's dict after construction cannot change the hypothesis, and in-place edits raise `TypeError`/`AttributeError`. `to_dict()` returns a fresh mutable plain-JSON copy. This is a data-integrity convention, not a security boundary.
- `parameters`, `generation_metadata`, `fitting_metadata`, and `uncertainty_metadata` are checked by `verify_no_ground_truth_leakage()` at construction, so a candidate cannot carry `ground_truth*`, `secret_*`, `true_*`, `evaluator_only*`, or `oracle_internal*` keys.
- `status` must be a `HypothesisStatus` member; raw strings are rejected.

`HypothesisOrigin` values: `system_generated`, `researcher_proposed`, `refined`.
Validation requires a `generator` for system-generated candidates, a
`parent_hypothesis_id` for refined candidates, and forbids a `generator` on
researcher-proposed candidates so a submission cannot claim to be generated.

The oracle domain label (`1d_polynomial`) and the registered family identifier
(`polynomial`) are intentionally distinct. Benchmark code must compare canonical
specifications or declared degree rather than assuming the two strings are equal
(Issue #24).

### 4.2 Supported model family: bounded polynomials

Degrees 0 through a configured maximum. Constant (degree 0) and linear (degree 1)
are the same implementation, not separate families.

| Setting | Value |
|---|---|
| `family` | `polynomial` |
| Default maximum degree | 3 (`DEFAULT_MAX_POLYNOMIAL_DEGREE`) |
| Hard ceiling | 12 (`HARD_MAX_POLYNOMIAL_DEGREE`), at most 13 coefficients |
| Input / output variables | `x` / `y` |
| Complexity | `degree + 1` |

Canonical specification (ascending order: `coefficients[i]` multiplies `x**i`):

```json
{
  "family": "polynomial",
  "degree": 2,
  "coefficient_order": "ascending",
  "coefficients": {"c0": 1.0, "c1": 2.0, "c2": 3.0},
  "input_variable": "x",
  "output_variable": "y"
}
```

This means `y = 1.0 + 2.0*x + 3.0*x^2`.

Validation rules:
- `degree` and coefficients must be `int`/`float`; booleans are rejected as non-numeric.
- Coefficients must be finite; NaN and infinity are rejected.
- All coefficients `c0..c_degree` must be present; missing, extra, or duplicate indices are rejected.
- `coefficient_order` must be `ascending`.
- For `degree >= 1` the leading coefficient must be non-zero. A model with fewer terms must declare a lower degree; this keeps `degree` honest and makes canonical equality a safe duplicate rule. Degree 0 may use any finite constant, including `0.0`.
- Unknown specification keys are rejected instead of silently dropped, so a specification cannot smuggle an expression or evaluator field.
- No expression parser, `eval`, `exec`, dynamic import, or unsafe deserialization is used anywhere in this path.

Extension point: validated models implement the model-neutral `ValidatedModel` interface
(`to_specification`, `canonical_key`, `summarize`, `complexity`), and families only
validate specifications and predict. A later non-polynomial family therefore never
has to fit polynomial-specific method signatures, and the registry, generator, and
consumers keep working unchanged.

### 4.3 Prediction interface

`PolynomialFamily.predict(specification, input_values)` and
`ModelFamilyRegistry.predict(...)` return `(prediction, error)`:

- Success: `({"y": 4.5}, None)`; the output mapping matches `Observation.output_values`.
- Failure: `(None, StructuredError(...))`.
- Inputs must be a mapping containing the family's input variable as a finite `int`/`float`; booleans, strings, missing keys, NaN, and infinity are rejected.
- Non-finite results, including float overflow, are rejected with `NON_FINITE_PREDICTION` rather than returned or clipped.
- Prediction is deterministic and never coerces or mutates inputs.
- Families also declare their scalar numeric task variables through `numeric_task_variables(specification)`. The base family returns `UNSUPPORTED_MODEL_FAMILY` rather than guessing, so fitting/evaluation orchestration stays model-neutral and never assumes a polynomial shape.

### 4.4 Candidate generation

`HypothesisGenerationConfig`:

| Field | Default | Meaning |
|---|---|---|
| `family_id` | `polynomial` | Registered family to sample. |
| `min_degree` / `max_degree` | 0 / 3 | Inclusive degree bounds; `max_degree` may not exceed the family maximum. |
| `candidate_count` | 6 | Unique candidates requested; 1..64. |
| `seed` | `None` | Seed for parameter draws; `None` is non-reproducible. |
| `coefficient_bound` | 1.0 | Uniform draws use `[-bound, bound]`; finite and >= 0. |
| `max_attempts_per_candidate` | 16 | Attempt budget multiplier before the space is declared exhausted. |

Algorithm:
1. One deterministic baseline per degree in `[min_degree, max_degree]`: zero coefficients except the leading term (`c_degree = 1.0`); degree 0 uses `c0 = 0.0`. These are interpretable starting points, not fits.
2. Seeded uniform draws until `candidate_count` unique candidates exist. Every draw is validated by the family; rejected or duplicate draws are skipped and consume attempt budget.
3. If the attempt budget is exhausted, generation raises `CandidateGenerationError` with `ATTEMPT_BUDGET_EXHAUSTED` instead of silently returning fewer candidates. The error reports bounded-sampling failure with the requested count, generated count, attempts used, and attempt budget; it does not claim that the candidate space is finite or empty.

Generated parameters are an **initialization policy**, not a fitted result.
Generation makes no accuracy, confidence, or ranking claim and leaves `status`
as `candidate`. Fitting is Issue #6.

Reproducibility: with a fixed seed the same specifications are produced in the same
order. `hypothesis_id` values stay unique per batch (UUID-based) and are therefore
not part of seed reproducibility.

### 4.5 Duplicate and equivalence semantics

- Duplicates are detected by canonical specification equality: family, degree, ascending coefficient indices and float values, and variable names.
- Numeric values normalize to `float`, so `1` and `1.0` are the same coefficient.
- Because a non-zero leading coefficient is required, canonical equality also covers functional equality for this family: a higher degree with zero leading terms is rejected rather than treated as equivalent.
- Not detected: symbolic equivalence across families, algebraically rewritten forms, or approximate numeric equality. This is a documented limitation, not general symbolic equivalence.
- Duplicates are removed within one generation batch only. Different provenance is never merged: a separate batch or a researcher proposal never collapses into an existing candidate.

### 4.6 Structured errors

| Code | Raised when |
|---|---|
| `UNKNOWN_MODEL_FAMILY` | Family identifier is missing from the registry. |
| `INVALID_MODEL_SPECIFICATION` | Specification shape, keys, order, or degree/coefficient agreement is invalid. |
| `MISSING_PARAMETER` | Required degree, coefficient, family, or prediction input is absent. |
| `INVALID_PARAMETER_TYPE` | Non-numeric or boolean value where a real number is required. |
| `NON_FINITE_VALUE` | Coefficient or prediction input is NaN/infinity. |
| `UNSUPPORTED_DEGREE` | Degree exceeds the family or configured maximum. |
| `NON_FINITE_PREDICTION` | Computed prediction is NaN/infinity, including overflow. |
| `INVALID_GENERATION_CONFIG` | Generation configuration is unusable. |
| `ATTEMPT_BUDGET_EXHAUSTED` | Bounded sampling used its attempt budget before reaching the requested count. Not proof that the candidate space is empty. |

Failures raise `CandidateGenerationError` carrying the same `StructuredError`
contract used by `OracleQueryError`, so API layers can serialize both consistently.

### 4.7 Isolation guarantee

`app.domain.candidate_generator` depends only on `app.domain.contracts` and
`app.domain.hypothesis_models`. It does not import the oracle adapter or the
benchmark evaluator, accepts no oracle/evaluator argument, and needs no database,
network, credentials, or Nodus access. `tests/test_hypotheses.py` verifies this in
a subprocess by importing the module and asserting that no `app.adapters*` or
`app.evaluator*` module was loaded.

This is dependency-level separation, not a security sandbox: code in the same
process could import anything. The guarantee is that the generation path has no
hidden-rule input and no evaluator dependency.

### 4.8 Example generated candidate (illustrative, not a measurement)

```json
{
  "hypothesis_id": "hyp-1f3c9a2b7d",
  "model_family": "polynomial",
  "formula": "y = 0.25 - 0.75*x + 1.5*x^2",
  "parameters": {
    "family": "polynomial",
    "degree": 2,
    "coefficient_order": "ascending",
    "coefficients": {"c0": 0.25, "c1": -0.75, "c2": 1.5},
    "input_variable": "x",
    "output_variable": "y"
  },
  "complexity": 3,
  "revision": 1,
  "created_at": "2026-10-20T00:00:00.000000+00:00",
  "status": "candidate",
  "provenance": {
    "origin": "system_generated",
    "generator": "polynomial_candidate_generator",
    "generator_version": "1.0.0"
  },
  "generation_metadata": {
    "generator": "polynomial_candidate_generator",
    "generator_version": "1.0.0",
    "seed": 7,
    "configuration": {
      "family_id": "polynomial",
      "min_degree": 0,
      "max_degree": 3,
      "candidate_count": 6,
      "seed": 7,
      "coefficient_bound": 1.0,
      "max_attempts_per_candidate": 16
    },
    "initialization": "seeded_uniform_random",
    "sequence_index": 4
  }
}
```

IDs, coefficients, and timestamps above are illustrative. They are not fitted
values, measured results, or evidence of discovery.

### 4.9 Tests

```bash
cd backend
python -m pytest tests/test_hypotheses.py -q   # focused
python -m pytest -q                            # full backend regression
```

### 4.10 Out of scope for Issue #5

Model fitting/optimization (#6), ranking and evidence tracking (#7), the discovery
loop (#8), experiment generation/selection (#9, #10), oracle querying, MySQL
persistence (#18), Flask routes (#19), Nodus integration (#12), and React UI
(#21, #22).

---

## 5. Fitting and Evaluation Pipeline (Issue #6)

**Code references:**
- [backend/app/domain/fitting.py](../backend/app/domain/fitting.py) - fitting interface, polynomial least squares, dispatch
- [backend/app/domain/evaluation.py](../backend/app/domain/evaluation.py) - metrics, observation validation, train/held-out orchestration
- [backend/tests/test_evaluation.py](../backend/tests/test_evaluation.py) - focused tests

**Scientific rule:** training observations fit the model; held-out observations only
test generalization. A low training error is not proof that a candidate is correct,
and a top training fit is not a discovered rule.

### 5.1 Public interfaces

```python
FittingConfig(method="least_squares", rank_tolerance=1e-10)
fit_candidate(candidate, training_samples, config=None, registry=None, fitters=None) -> FittingResult

EvaluationPipeline(registry=None, fitters=None, fitting_config=None)
pipeline.evaluate(request, observations, dataset_id=None) -> EvaluationResult
pipeline.evaluate_many(requests, observations, dataset_id=None) -> list[EvaluationResult]
```

- Family fitters implement `FamilyFitter.fit(...)`; model-family-specific numerics stay
  inside the fitter. Orchestration in `EvaluationPipeline` is model-neutral.
- `fitters` is an injection point for tests and future families; the default covers
  `polynomial` only.
- Fitters receive only validated training samples. `fit_candidate` cannot be handed
  held-out observations at all.

### 5.2 Training and held-out semantics

- `EvaluationRequest.observation_ids` is the training partition; `held_out_observation_ids`
  is the held-out partition. The contract already rejects overlap.
- The pipeline also rejects duplicate IDs inside a partition and missing/duplicate metric names.
- Fitting runs once, on training samples only. Training metrics and held-out metrics are
  computed from the same fitted specification; held-out evaluation never refits or mutates it.
- A failed fit returns `status=failed` with no metrics and no fitted specification, so an
  unfitted candidate cannot look favorable.
- No candidate ranking or model selection happens here. Reporting held-out metrics is not
  selecting on them.

### 5.3 Polynomial least-squares policy

- Method: ordinary least squares via Householder QR on the Vandermonde design matrix,
  implemented without external dependencies. Coefficients stay in ascending order
  (`c0`..`c_degree`).
- Conditioning: the fit is solved in a scaled input `t = (x - center) / scale` built from the
  training inputs, then converted exactly back to the `x` basis.
- Minimum data: `n >= degree + 1`. Underdetermined data is `INSUFFICIENT_OBSERVATIONS`.
- Exactly determined data (`n == degree + 1`) is allowed, reported with metadata
  `exactly_determined` and the warning `EXACTLY_DETERMINED_FIT`; the fit has no residual
  degrees of freedom and must not be read as validation.
- Rank deficiency is detected on the relative QR diagonal (`rank_tolerance`). It fails as
  `RANK_DEFICIENT_FIT`; the degree is never silently reduced.
- A zero leading coefficient is `DEGENERATE_FIT`, not a lower-degree model.
- Non-finite samples or fitted coefficients fail explicitly. Fitting has no randomness, so
  identical data and configuration reproduce identical parameters.

### 5.4 Observation acceptance and exclusions

Only `ObservationStatus.SUCCESS` observations with finite, non-boolean numeric values for
both task variables are accepted. Everything else is excluded and counted with a reason code:

| Code | Cause |
|---|---|
| `missing_observation` | Requested ID not present in the supplied observations. |
| `invalid_observation_object` | Value is not an `Observation`. |
| `observation_<status>` | Non-success status (e.g. `observation_oracle_failure`). |
| `invalid_input_payload` / `invalid_output_payload` | Values are not mappings. |
| `missing_input_value` / `missing_output_value` | Task variable absent. |
| `invalid_input_type` / `invalid_output_type` | Non-numeric or boolean value. |
| `non_finite_input` / `non_finite_output` | NaN or infinity. |

Exclusions are reported in `observation_counts` and `exclusion_codes`; they are never
treated as numeric zeros and never silently dropped. Duplicate input values are retained as
independent samples: repeated measurements at the same input are legitimate noisy evidence
and are not deduplicated in this issue.

### 5.5 Metrics

For `n` accepted paired values `(actual_i, predicted_i)` for a single output variable:

- **MAE** = `mean(|actual_i - predicted_i|)`
- **RMSE** = `sqrt(mean((actual_i - predicted_i)^2))`

Known answer: actuals `[3, 5]` and predictions `[2, 7]` give MAE `1.5` and RMSE
`sqrt(2.5) = 1.5811388300841898`.

Rules:
- Empty datasets, mismatched lengths, booleans, strings, NaN/infinity, and non-finite
  intermediates are structured metric failures. Metrics are never reported as `0` for an
  empty or invalid dataset.
- Metrics are computed for a single scalar output variable. Multi-output aggregation is not
  implemented; combining different output scales into one score would be misleading.
- Metric names and definitions are recorded on the result (`metric_definitions`,
  `metric_version`). `SUPPORTED_METRICS` is the extension point for future metrics.

### 5.6 Standardized result schema

`EvaluationResult` adds these fields (all optional, absent when unavailable):

| Field | Meaning |
|---|---|
| `model_family` | Registered family identifier. |
| `fitting_status` | `success` or `failed`. |
| `fitting_method` / `fitting_config` | Method and configuration that produced the fit. |
| `fitted_specification` | Fitted canonical specification; present only when fitting succeeded. |
| `complexity` | Complexity of the fitted model. |
| `sample_count` | Accepted training observations used for fitting. |
| `held_out_sample_count` | Accepted held-out observations used for metrics. |
| `observation_counts` | Requested/accepted/excluded counts for both partitions. |
| `exclusion_codes` | Aggregated exclusion reason counts. |
| `metric_definitions` / `metric_version` | Metric semantics and version. |
| `dataset_ids` | Dataset identifier (when supplied) and partition observation IDs. |
| `request_id` | Evaluation request that produced the result. |
| `warnings` | Non-fatal structured warnings (e.g. `HELD_OUT_UNAVAILABLE`). |

Absent metrics mean unavailable, never zero. Failed results carry `metrics = {}`, no
`fitted_specification`, and a structured `error`. The original candidate is never mutated;
downstream ranking builds its own fitted hypothesis from `fitted_specification` if it needs one.

### 5.7 Status and error codes

- Statuses: `EvaluationStatus.completed|failed`, `FittingStatus.success|failed`.
- Fitting codes: `INVALID_FITTING_CONFIG`, `UNSUPPORTED_FITTING_METHOD`,
  `INSUFFICIENT_OBSERVATIONS`, `RANK_DEFICIENT_FIT`, `DEGENERATE_FIT`,
  `NON_FINITE_COEFFICIENT`, `FIT_SPECIFICATION_INVALID`, `FITTING_NUMERICAL_FAILURE`,
  `UNKNOWN_MODEL_FAMILY`, `UNSUPPORTED_MODEL_FAMILY`.
- Evaluation codes: `INVALID_EVALUATION_REQUEST`, `UNSUPPORTED_METRIC`, `EMPTY_DATASET`,
  `DATASET_LENGTH_MISMATCH`, `INVALID_METRIC_INPUT`, `NON_FINITE_METRIC_INPUT`,
  `NON_FINITE_METRIC`, `PREDICTION_FAILURE`, `METRIC_COMPUTATION_FAILURE`,
  `UNEXPECTED_EVALUATION_ERROR`.
- Warnings: `EXACTLY_DETERMINED_FIT`, `HELD_OUT_UNAVAILABLE`.

### 5.8 Leakage safeguards and limits

Implemented guarantees, each covered by tests:

- **A - held-out targets cannot affect fitted parameters:** changing held-out outputs leaves
  `fitted_specification` and training metrics unchanged.
- **B - the fitter sees training only:** a recording fitter asserts the exact observation IDs
  passed into fitting equal the training partition.
- **E - no refit on held-out data:** exactly one fit call happens per evaluation.

Invariant D (no selection on held-out scores) is satisfied by omission: this module has no
ranking or selection API. Ranking is Issue #7.

These are data-flow guarantees inside one process, not a security sandbox.

### 5.9 Reproducibility

Given the same observations, partitions, fitting configuration, and deterministic method,
evaluation reproduces the same fitted specification and metric values. Result and evaluation
IDs remain unique per call. Random train/held-out splitting is not implemented: callers supply
explicit partitions or fixed benchmark datasets.

### 5.10 Tests

```bash
cd backend
python -m pytest tests/test_evaluation.py -q   # focused
python -m pytest -q                            # full backend regression
```

### 5.11 Out of scope for Issue #6

Candidate ranking/selection (#7), the discovery loop (#8), experiment generation/selection
(#9, #10), uncertainty estimation, multi-output metrics, MySQL persistence (#18), Flask
routes (#19), Nodus integration (#12), and React UI (#21, #22).

---

## 6. Candidate Experiment Generation (Issue #9)

**Code references:**
- [backend/app/domain/experiment_generator.py](../backend/app/domain/experiment_generator.py) — `CandidateExperiment`, `NumericalDomainBounds`, `ExperimentGenerationConfig`, `CandidateExperimentGenerator`, `CandidateExperimentBatch`
- [backend/tests/test_experiment_generation.py](../backend/tests/test_experiment_generation.py) — comprehensive unit test suite

### 6.1 Representation and Interface
`CandidateExperiment` encapsulates a candidate input point evaluated before oracle query submission:
```json
{
  "candidate_id": "cand-exp-0a1b2c3d4e",
  "input_data": {
    "input_id": "inp-9f8e7d6c5b",
    "domain": "1d_polynomial",
    "values": {"x": 2.5},
    "metadata": {"strategy": "grid", "grid_index": 2}
  },
  "status": "eligible",
  "strategy": "grid",
  "created_at": "2026-10-21T00:00:00.000000+00:00",
  "generation_metadata": {"strategy": "grid", "point": 2.5}
}
```

### 6.2 Supported Domain Bounds
- `NumericalDomainBounds`: Specifies continuous box constraints for independent variables (e.g. `variable_name="x"`, `min_val=-10.0`, `max_val=10.0`).
- Validates that values are finite real numbers within inclusive bounds; rejects booleans, strings, NaNs, and infinities without leaking unhandled `OverflowError` on extreme integer representations.

### 6.3 Strategies
1. **`grid`:** Generates evenly spaced deterministic test points across `[min_val, max_val]`. Subsamples evenly across eligible points after exclusions to maintain global spatial coverage. Special-cases `pool_size=1` safely.
2. **`uniform_random`:** Seeded uniform draws within domain bounds; exactly reproducible across runs when `seed` is fixed.
3. **`stratified_random`:** Splits the domain into equal intervals and samples within each stratum to ensure broad spatial coverage.

### 6.4 Deduplication, Exclusion, and Diagnostics
- **Canonical Key:** Deduplicates candidates using `(("variable_name", round(value, precision_digits)), ...)`.
- **Exclusion of Queried Inputs:** Automatically filters out previously observed inputs using pure absolute tolerance (`rel_tol=0.0`, `abs_tol=tolerance`, default `1e-5`).
- **Batch Diagnostics:** Returns `CandidateExperimentBatch` exposing `requested_count`, `generated_count`, `is_undersized`, and `diagnostic_message`.
- **Structured Error Handling:** Raises `CandidateExperimentGenerationError` with `EMPTY_CANDIDATE_POOL` or `UNDERSIZED_CANDIDATE_POOL` if no valid candidate points remain and `allow_undersized=False`.

### 6.5 Scientific Isolation Guarantee
The experiment generator imports only domain contracts; it has zero dependencies on `app.adapters.oracle` or `app.evaluator` and does not access ground truth.

---

## 7. Discovery Loop (Issue #8)

**Code references:**
- [backend/app/domain/discovery_engine.py](../backend/app/domain/discovery_engine.py) — `DiscoveryEngine`
- [backend/app/domain/discovery_contracts.py](../backend/app/domain/discovery_contracts.py) — run configuration, lifecycle statuses, history records, planner protocol, `DiscoveryRunResult`
- [docs/discovery-loop.md](discovery-loop.md) — architecture, lifecycle, and limitations
- [backend/tests/test_discovery_loop.py](../backend/tests/test_discovery_loop.py) — integration suite

### 7.1 Run configuration (`DiscoveryRunConfig`)
```json
{
  "max_query_budget": 4,
  "max_iterations": 4,
  "stopping_criteria": {
    "target_plausible_candidate_count": null,
    "target_primary_metric_threshold": null,
    "consecutive_failure_limit": 3
  },
  "domain_bounds": {"variable_name": "x", "min_val": -2.0, "max_val": 2.0, "domain_name": "1d_polynomial"},
  "hypothesis_config": {"family_id": "polynomial", "min_degree": 0, "max_degree": 3, "candidate_count": 6, "seed": 7},
  "fitting_config": {"method": "least_squares", "rank_tolerance": 1e-10},
  "ranking_policy": {"primary_metric": "mae", "secondary_metric": "rmse", "metric_direction": "minimize", "performance_acceptance_threshold": null},
  "experiment_pool_size": 5,
  "experiment_strategy": "grid",
  "allow_duplicate_queries": false,
  "allow_duplicate_external_observations": false,
  "query_exclusion_tolerance": 1e-05,
  "experiment_seed": null,
  "dataset_id": null,
  "max_experiment_candidates_per_iteration": 25
}
```
Invalid values (negative budget, `max_iterations < 1`, pool size outside 1..500,
non-finite tolerance, wrong types) raise `ValueError` during construction, before
any oracle call. `max_query_budget = 0` is valid and performs no queries.

### 7.2 Lifecycle and termination
`DiscoveryLifecycleStatus`: `initializing`, `running`,
`terminated_stopping_criterion`, `terminated_budget_exhausted`,
`terminated_max_iterations`, `terminated_no_experiments`, `failed`.

`DiscoveryTerminationReason`: `stopping_criterion_plausibility_reached`,
`stopping_criterion_metric_threshold_reached`, `budget_exhausted`,
`max_iterations_reached`, `experiment_pool_exhausted`,
`consecutive_oracle_failures`, `component_failure`, `unrecoverable_error`.

Deterministic precedence at the start of each iteration: satisfied configured
stopping criterion → budget exhausted → max iterations → no valid experiment.
Component/oracle failures terminate immediately when they occur. No stopping
criterion is inferred: ranking first is not success, and a policy without
`performance_acceptance_threshold` cannot produce `PLAUSIBLE`.

The metric-threshold criterion applies only to the best candidate the ranking
policy already accepts as `PLAUSIBLE` in a rankable comparability tier, so a low
metric value alone can never terminate a run as a scientific success.

**Terminal immutability and resume.** A finalized run is immutable by default:
`add_observation()` raises `RUN_NOT_ACTIVE`. `resume(observations)` explicitly
reopens a terminated, non-failed run: it validates the batch, preserves IDs,
ingests without consuming oracle budget, refreshes evaluation/ranking, and
continues the bounded loop without resetting budget or iteration counters.
`FAILED` runs are not resumable (`RUN_TERMINAL_FAILURE`), and resuming before a
run finishes raises `RUN_NOT_FINISHED`. Duplicate policy is split:
`allow_duplicate_queries` governs oracle re-querying;
`allow_duplicate_external_observations` governs repeated external inputs.

### 7.3 Query accounting (`QueryAccounting`)
```json
{
  "max_query_budget": 4,
  "attempted_queries": 4,
  "successful_queries": 4,
  "failed_queries": 0,
  "remaining_queries": 0
}
```
`attempted_queries` counts every `oracle.query` call. Raised, non-success, and
contract-invalid responses consume budget; pre-invocation rejections, duplicate
skips, and external ingestion (`add_observation`/`resume`) do not. The invariant
`attempted_queries <= max_query_budget` holds on every path, and
`successful_queries + failed_queries == attempted_queries`.

### 7.4 Experiment planner boundary
```python
class ExperimentPlanner(Protocol):
    def select_experiment(
        self,
        candidates: Sequence[HypothesisRepresentation],
        evaluations: Mapping[str, EvaluationResult],
        observations: Sequence[Observation],
        candidate_batch: CandidateExperimentBatch,
        ranking: Optional[RankingResult],
    ) -> Optional[CandidateExperiment]: ...
```
`DeterministicOrderPlanner` (default) returns the first eligible candidate and
uses no scientific scoring. Active disagreement selection (Issue #10) is opt-in
through `ExperimentSelectionPlanner`; information-gain selection (#11) remains
deferred.

Backwards-compatible diagnostics extension: a planner may additionally expose
`select_experiment_with_diagnostics` with the same argument list and return an
`ExperimentSelectionResult`. The engine prefers it when present and stores
`result.to_dict()` in `ExperimentExecutionRecord.selection_diagnostics`; the
required `select_experiment` method and its return type are unchanged. A planner
never receives the oracle or evaluator-only data.

### 7.5 Result and history (`DiscoveryRunResult`)
Top-level serialized fields: `run_id`, `status`, `termination_reason`, `config`,
`iterations_completed`, `ranking_is_fresh`, `started_at`, `finished_at`,
`query_accounting`, `hypotheses`, `evaluations`, `ranking`, `observations`,
`experiment_history`, `iteration_history`, `ingestion_history`, `errors`,
`warnings`, `limitations`.

```json
{
  "run_id": "run-1a2b3c4d5e",
  "status": "terminated_budget_exhausted",
  "termination_reason": "budget_exhausted",
  "iterations_completed": 2,
  "ranking_is_fresh": true,
  "query_accounting": {"max_query_budget": 2, "attempted_queries": 2, "successful_queries": 2, "failed_queries": 0, "remaining_queries": 0},
  "experiment_history": [
    {
      "sequence": 1,
      "iteration": 1,
      "experiment_id": "cand-exp-1a2b3c4d5e",
      "input_id": "inp-9f8e7d6c5b",
      "input_values": {"x": -2.0},
      "outcome": "invoked_success",
      "observation_id": "obs-0a1b2c3d4e",
      "oracle_sequence_number": 1,
      "strategy": "grid",
      "selection_diagnostics": null,
      "recorded_at": "2026-10-21T00:00:00.000000+00:00"
    }
  ],
  "iteration_history": [
    {
      "iteration": 1,
      "selected_experiment_id": "cand-exp-1a2b3c4d5e",
      "observation_id": "obs-0a1b2c3d4e",
      "outcome": "invoked_success",
      "attempted_queries_before": 0,
      "attempted_queries_after": 1,
      "successful_queries_after": 1,
      "failed_queries_after": 0,
      "top_hypothesis_id": "hyp-003",
      "top_primary_metric": 0.0,
      "ranking_is_fresh": true,
      "recorded_at": "2026-10-21T00:00:00.000000+00:00"
    }
  ],
  "ingestion_history": [],
  "errors": [],
  "warnings": [],
  "limitations": []
}
```
IDs, metric values, and timestamps above are illustrative. `termination_reason`
is `null` only for a snapshot taken while the run is still active. `to_dict()`
returns a JSON-compatible payload and re-runs `verify_no_ground_truth_leakage`;
a forbidden key raises `ValueError` instead of being serialized.

Snapshots are deep, not shallow. Observation payloads in the engine and in a
returned result are frozen read-only mappings, so nested mutation raises
`TypeError`. Observations supplied by callers are copied at commit. Returned
`EvaluationResult` snapshots freeze every nested mapping as well (`metrics`,
`held_out_metrics`, `fitting_config`, `fitted_specification`,
`observation_counts`, `exclusion_codes`, `metric_definitions`, `dataset_ids`,
plus error/warning `details`), so post-run mutation cannot alter committed
evidence, evaluations, ranking, or the serialized result.

### 7.6 Failure semantics
A successful observation is committed immediately. If the subsequent
evaluation/ranking refresh raises, the observation stays committed, the previous
evaluation and ranking snapshots are preserved, `ranking_is_fresh` becomes
`false`, and the run ends `failed` / `component_failure`. Invalid oracle
responses and non-success statuses are never committed and always consume the
query they used. No automatic oracle retries are performed.

Structured codes include: `CANDIDATE_GENERATION_FAILURE`,
`EXPERIMENT_GENERATION_FAILURE`, `EVALUATION_FAILURE`, `RANKING_FAILURE`,
`PLANNER_FAILURE`, `INVALID_PLANNER_SELECTION`, `DUPLICATE_QUERY`,
`ORACLE_QUERY_FAILURE`, `INVALID_ORACLE_RESPONSE`, `INVALID_OBSERVATION`,
`INVALID_INITIAL_OBSERVATION`, `DUPLICATE_OBSERVATION_ID`,
`DUPLICATE_OBSERVATION_INPUT`, `RUN_NOT_ACTIVE`, `RUN_NOT_FINISHED`,
`RUN_TERMINAL_FAILURE`, `UNEXPECTED_DISCOVERY_ERROR`; warnings include
`DUPLICATE_QUERY_SKIPPED`, `EXPERIMENT_POOL_UNDERSIZED`, `RANKING_STALE`.
Evaluation results must contain exactly one entry per requested hypothesis ID;
duplicate, unknown, or missing IDs fail the refresh instead of being accepted.

### 7.7 Ground-truth isolation
The engine receives only `OracleInterface.validate_input` / `query`. It never
imports `GroundTruthRule`, never calls evaluator-only oracle accessors, and
passes only candidates, evaluations, observations, experiment batches, and the
ranking to the planner. Tests assert the dependency wiring and that a canary
value held in evaluator-only metadata never appears in results, history,
errors, warnings, or planner arguments.

### 7.8 Tests

```bash
cd backend
python -m pytest tests/test_discovery_loop.py -q   # focused
python -m pytest -q                                # full backend regression
```

### 7.9 Out of scope for Issue #8
Information-gain selection (#11), held-out benchmark procedures, MySQL
persistence (#18), Flask routes (#19), Nodus integration (#12), and React UI
(#21, #22). Active disagreement selection is specified in §8 and
[experiment-selection.md](experiment-selection.md).

---

## 8. Active Experiment Selection (Issue #10)

**Code references:**
- [backend/app/domain/selection_contracts.py](../backend/app/domain/selection_contracts.py) — strategies, metrics, configuration, per-candidate diagnostics, selection result
- [backend/app/domain/experiment_selection.py](../backend/app/domain/experiment_selection.py) — `ActiveExperimentSelector`, `ExperimentSelectionPlanner`, `RandomBaselinePlanner`, `compute_disagreement`
- [docs/experiment-selection.md](experiment-selection.md) — algorithm, examples, and limitations
- [backend/tests/test_experiment_selection.py](../backend/tests/test_experiment_selection.py) — focused tests

### 8.1 Selection result (`ExperimentSelectionResult`)
```json
{
  "selection_version": "1.0.0",
  "status": "selected",
  "strategy": "active_disagreement",
  "reason_code": "active_disagreement_max",
  "rationale": "Selected candidate cand-exp-x2 at {'x': 2.0} with the maximum population_variance disagreement score (1.0) across 2 eligible hypothesis(es).",
  "config": {
    "strategy": "active_disagreement",
    "disagreement_metric": "population_variance",
    "min_eligible_hypotheses": 2,
    "exclude_queried_inputs": true,
    "exclusion_tolerance": 1e-05,
    "fallback_strategy": "none",
    "random_seed": null,
    "uncertainty_weight": 0.0,
    "allow_candidate_specifications": true
  },
  "candidate_pool_size": 2,
  "valid_candidate_count": 2,
  "eligible_hypothesis_count": 2,
  "eligible_hypothesis_ids": ["hyp-linear", "hyp-quadratic"],
  "excluded_hypotheses": {},
  "excluded_candidates": {},
  "candidates": [
    {"candidate_id": "cand-exp-x2", "input_values": {"x": 2.0},
     "scoring_status": "scored", "disagreement_score": 1.0,
     "usable_predictions": {"hyp-linear": 2.0, "hyp-quadratic": 4.0},
     "excluded_predictions": {}, "usable_prediction_count": 2}
  ],
  "selected_candidate_id": "cand-exp-x2",
  "selected_input_values": {"x": 2.0},
  "tie_break_policy": "highest disagreement score, then lexicographically smallest canonical input key (numeric values ascending), then smallest candidate_id",
  "tie_break_applied": false,
  "tied_candidate_ids": ["cand-exp-x2"],
  "prediction_source": "candidate_specification",
  "random_seed": null,
  "uncertainty": {"available": false, "source": null, "reason": "..."}
}
```
`status` is `selected`, `fallback_selected`, or `no_selection`. `reason_code`
distinguishes empty pool, no eligible candidates, all inputs previously queried,
insufficient hypotheses, no usable predictions, disagreement overflow, zero
disagreement, the active maximum, the random draw, and the explicit sequential
fallback. `to_dict()` is JSON-compatible, deterministic for identical inputs and
configuration, and re-runs `verify_no_ground_truth_leakage`.

### 8.2 Supported disagreement metrics
Population variance (default, divides by `n`), sample variance (divides by
`n - 1`), and peak-to-peak maximum spread, over finite scalar predictions from
eligible hypotheses. Variance is computed from centered deviations scaled by
their own magnitude and rescaled with `math.frexp`/`math.ldexp`, avoiding both
`scale * scale` overflow and precision loss for small relative spreads at large
magnitudes. Scores are raw and pool-normalization is never applied: an all-zero
pool returns `zero_disagreement_pool` rather than manufactured spread. A
genuinely unrepresentable score is reported as `disagreement_overflow`, never as
zero or infinity.

### 8.3 Eligibility and prediction source
Ranking `CONTRADICTED`/`UNEVALUABLE` hypotheses and hypotheses missing from a
supplied ranking are excluded. Fitted specifications are used when at least
`min_eligible_hypotheses` valid fits exist; explicit
`allow_candidate_specifications=true` switches the whole decision to validated
candidate specifications when too few fits exist. Fitted and unfitted
predictions are never mixed in one score. Missing, malformed, non-finite, or
unsupported predictions are recorded per hypothesis and never replaced with
zero.

### 8.4 Determinism, duplicates, and the random baseline
Ties are ordered by canonical input key then candidate ID; batch order cannot
change the outcome. A committed input excludes a candidate only when both have
identical input variable names and every numeric value satisfies pure absolute
tolerance (`rel_tol=0.0`); boolean/string values must match exactly, and
different or partially overlapping schemas are not duplicates. This applies to
both strategies, and the engine's duplicate guard remains the final safety net.
`random_baseline` requires an integer seed, uses a local `random.Random`, and
draws from the same valid pool without consulting disagreement. An explicit
`sequential` fallback is the only fallback and is labelled `fallback_selected`.

### 8.5 Uncertainty and isolation
No fit/evaluation contract exposes a validated prediction-covariance,
residual-variance, or interval estimate, so the result reports
`uncertainty.available=false` with a reason and `uncertainty_weight` must be
`0.0`. The selector imports no oracle/evaluator module, receives only planner
arguments, and stores only discovery-side data. Tests assert dependency wiring,
the planner argument list, and canary isolation.

### 8.6 Tests
```bash
cd backend
python -m pytest tests/test_experiment_selection.py -q
python -m pytest -q
```

---

## 9. LLM Scientist (Issue #41)

**Code references:**
- [backend/app/domain/llm_scientist_contracts.py](../backend/app/domain/llm_scientist_contracts.py) — configuration, request, provider protocol, call result, rejection/round records
- [backend/app/domain/llm_scientist.py](../backend/app/domain/llm_scientist.py) — versioned prompt, bounded evidence context, local validation, canonicalization, round orchestration
- [backend/app/adapters/llm/groq_provider.py](../backend/app/adapters/llm/groq_provider.py) — Groq runtime adapter
- [docs/llm-scientist.md](llm-scientist.md) — setup, fallback semantics, benchmark, limitations

### 9.1 Trust boundary
`LLMScientistConfig` defaults to `enabled=false`. When disabled, the discovery
engine is constructed without a scientist, no provider is created, and no
network call is possible. Provider output is untrusted data: it is parsed as
strict JSON, validated into the existing Issue #5 hypothesis contract, and can
never execute code, call an oracle, or mutate committed state.

### 9.2 Proposal request (`LLMProposalRequest`)
Frozen and leakage-checked. Permitted fields only:

```json
{
  "request_id": "llmreq-...",
  "round_index": 1,
  "prompt_version": "1.0.0",
  "schema_version": "1.0.0",
  "domain": "1d_polynomial",
  "variable_name": "x",
  "output_variable": "y",
  "domain_bounds": {"min_val": -2.0, "max_val": 2.0},
  "model_family": "polynomial",
  "max_polynomial_degree": 3,
  "max_proposals": 3,
  "observations": [{"sequence_number": 1, "input_values": {"x": -1.0}, "output_values": {"y": 1.0}}],
  "hypotheses": [{"hypothesis_id": "hyp-...", "formula": "...", "parameters": {},
                   "evaluation": {"status": "completed", "metrics": {}},
                   "ranking": {"rank": 1, "status": "uncertain"}}],
  "ranking_summary": {"available": true, "ranked_hypothesis_ids": []},
  "existing_canonical_keys": ["{...}"],
  "previous_rounds": [],
  "failure_summaries": []
}
```
Observations expose only `sequence_number`, `input_values`, and
`output_values`. No oracle internals, evaluator labels, hidden rules, benchmark
answers, credentials, or previous raw prompts are sent.

### 9.3 Provider result (`ProviderCallResult`)
`status` is one of `success`, `disabled`, `not_configured`,
`missing_credentials`, `sdk_unavailable`, `authentication_failed`,
`permission_denied`, `rate_limited`, `timeout`, `transient_failure`,
`provider_error`, `malformed_response`, `response_too_large`. `attempts` counts
real attempts (`0` when no call was possible). `usage` is present only when the
provider actually returned token metadata; cost/latency are never invented.
`is_live` is `false` for every fake provider, so offline tests cannot be
reported as live execution. `credential_slot` is the 1-based configured key slot
that produced the final outcome; credential values are never recorded. With
multiple configured slots, a rate-limited slot rotates to the next slot (bounded
by `slots × (1 + max_retries)` attempts); other failure classes return
immediately.

### 9.4 Proposal schema and rejection codes
Required: `model_family` (only `polynomial`), `degree` (integer inside the
run's configured maximum), `coefficients` (exactly `degree + 1` finite numbers,
ascending powers), and non-empty `rationale`. Optional and bounded:
`assumptions`, `competing_explanations`, `distinguishing_evidence`,
`suggested_experiment_inputs` (inside domain bounds), and
`self_reported_confidence` (`[0, 1]`, recorded as uncalibrated).

Rejection codes: `malformed_response`, `schema_invalid`, `missing_field`,
`invalid_type`, `unsupported_model_family`, `unsupported_representation`,
`unsafe_field`, `non_finite_value`, `out_of_domain`, `too_many_proposals`,
`duplicate_proposal`, `invalid_suggestion`. Unknown fields are rejected; fields
implying code/tool execution are rejected explicitly. Above
`max_proposals_per_round` the whole round is rejected. Duplicates are detected by
the model family's canonical key against existing candidates and earlier
proposals in the same round.

### 9.5 Round record (`ScientistRoundRecord`)
`round_id`, `round_index`, `run_id`, `provider_id`, `model_id`,
`prompt_version`, `schema_version`, `status`
(`success|no_valid_proposals|provider_failure|disabled`), timestamps, measured
`duration_ms`, `proposals_returned`, `accepted_count`, `rejected_count`,
`accepted_hypothesis_ids`, structured `rejected` entries, `outcome_status`,
`error`, `attempts`, real `usage`, `is_live_provider`, `credential_slot`,
`fallback_applied`, `fallback_reason`, and an evidence summary. Every non-success round must record
its fallback. The record is exposed in
`DiscoveryRunResult.scientist_history` and is leakage-checked on construction
and serialization.

### 9.6 Engine integration
`DiscoveryEngine(..., scientist=...)` runs at most one round after the initial
ranking pass and after each successful oracle query, bounded by
`LLMScientistConfig.max_rounds`. Rounds consume no query budget. Accepted
hypotheses are re-validated by the engine, then fitted, evaluated, ranked, and
used by the planner exactly like generated candidates. Advisory inputs enter the
next experiment batch through `ExperimentGenerationConfig.suggested_inputs`
(same bounds/exclusion/dedup checks, tagged
`ExperimentGenerationStrategy.SUGGESTED`) and never bypass the planner, oracle
validation, duplicate guard, budget, or stopping policy.

### 9.7 Failure and fallback semantics
Provider failures and empty rounds never fail the discovery run by themselves.
They produce a non-success round record plus a `SCIENTIST_FALLBACK` warning, and
the run continues with the existing candidates under the existing stopping
policy. `fallback_policy=disable_further_rounds` additionally suppresses later
provider calls. Deterministic fallback output is never labelled LLM-generated.

### 9.8 Isolation and tests
Evaluator-only ground truth is absent from requests, round records, warnings,
logs, and serialized results; both request and record construction fail closed
on forbidden keys. Tests assert canary isolation, immutable committed state
under malicious-looking provider output, and that no generated code is executed.

```bash
cd backend
python -m pytest tests/test_llm_scientist.py -q
python -m pytest -q
python -m benchmarks.llm_scientist_benchmark
```

