# CLARITY Scientific Data Contracts & Boundaries

**Document Version:** 1.1.0 (Issue #4 baseline + Issue #5 hypothesis extension)  
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
* **Evaluation Result:**
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
  "evaluated_at": "2026-10-20T03:00:20.000000+00:00"
}
```

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
| `status` | yes (default `candidate`) | `candidate` = proposed, not evaluated. `evaluated` is set by downstream evaluation (#6); the generator never sets it. |
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
