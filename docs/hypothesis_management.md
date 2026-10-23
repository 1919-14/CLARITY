# Human-in-the-Loop Hypothesis Management

This document specifies the architecture, supported syntax, validation rules, security invariants, and lifecycle semantics for human-in-the-loop hypothesis management in CLARITY (Issue #16).

---

## 1. Overview and Scientific Philosophy

CLARITY allows researchers to actively collaborate with automated discovery loops by:
- Proposing their own candidate hypotheses using natural, validated mathematical expressions.
- Inspecting candidate hypotheses and their full revision and evaluation histories without exposing ground-truth oracles.
- Flagging hypotheses of interest (**Pinning**) as a display preference without distorting objective scientific metrics or rankings.
- Removing unpromising candidates from active search consideration (**Exclusion**) while preserving their complete audit trail and prior evaluation records.
- Restoring previously excluded candidates (**Restoration**) if new experimental observations warrant reconsideration.
- Triggering hypothesis **Re-evaluation** against newly acquired or expanded observation datasets.

### Core Scientific Invariants

1. **Researcher Input is a Candidate, Not Pre-Validated Truth**:
   - Researcher-proposed hypotheses are recorded with `creation_source: "researcher"`.
   - They enter CLARITY's standard evaluation and ranking pipeline and are evaluated strictly against empirical observations.
   - Hypotheses are never reported as scientifically validated until evaluated and confirmed by empirical evidence.

2. **Pinning Never Distorts Scientific Scores**:
   - Pinning indicates human interest or UI prioritization (`is_pinned: true`).
   - Pinning **never** modifies metric scores ($MAE$, $RMSE$), complexity penalties, evidence classifications, or model rankings produced by `HypothesisRanker`.

3. **Exclusion Never Erases Scientific History**:
   - Excluding a hypothesis sets `is_excluded: true`, removing it from active candidate selection and query generation (`include_excluded=false`).
   - The candidate's database rows, revisions, evaluations, and audit events are permanently retained for scientific integrity and post-run retrospective analysis.

4. **Predictable Re-evaluation Invariants**:
   - Re-evaluation runs the hypothesis through the standard `EvaluationPipeline` against all currently available run observations.
   - Re-evaluation **never overwrites** historical evaluation records; it appends a new `EvaluationResultModel` with a distinct `evaluation_id`, capturing the latest `dataset_version`, `sample_count`, and metrics.
   - Excluded hypotheses cannot be re-evaluated; callers must restore the candidate first, preventing accidental evaluation of retired candidates.

---

## 2. Supported Hypothesis Syntax and Grammar

Researchers propose hypotheses through formulas conforming to standard mathematical notation.

### Supported Model Families

CLARITY supports numerical model families registered in `backend/app/domain/hypothesis_models.py`:
- `polynomial`: General bounded polynomials up to configured degree (hard ceiling: degree 12).
- `linear` / `1d_linear`: Linear models ($y = ax + b$).
- `quadratic`: Degree-2 polynomials ($y = ax^2 + bx + c$).
- `cubic`: Degree-3 polynomials ($y = ax^3 + bx^2 + cx + d$).
- `constant`: Degree-0 models ($y = c$).

### Formula Syntax Rules

Expressions may optionally specify an explicit left-hand side variable (e.g. `y = ...` or `output = ...`):

```text
# Explicit LHS:
y = 2.5*x^2 - 1.2*x + 4.0
y = 0.5x^2 - 1.2x + 3.0

# Expression RHS only (defaults output variable to 'y'):
2*x + 1
0.5*x**2 + 3.2
```

#### Allowed Mathematical Operators and Constructs

| Operator | Notation | Example | Notes |
| :--- | :--- | :--- | :--- |
| Addition | `+` | `x + 2` | Binary and unary (`+x`) |
| Subtraction | `-` | `x - 2` | Binary and unary (`-x`) |
| Multiplication | `*` or implicit | `2*x` or `2x` or `0.5x^2` | Implicit multiplication normalized |
| Division | `/` | `x / 2` | Division by literal 0 is rejected |
| Power | `^` or `**` | `x^2` or `x**3` | Exponent must be non-negative integer $\le 12$ |
| Grouping | `(...)` | `(x + 1) * (x - 2)` | Standard parentheses |

#### Parameter Coefficients

Parameters may be supplied explicitly in the request payload or inferred from the expression:
```json
{
  "model_family": "polynomial",
  "formula": "y = 2.0*x + 1.0",
  "parameters": {"c0": 1.0, "c1": 2.0},
  "complexity": 2
}
```
For linear models, shorthand parameters `{"a": 2.0, "b": 1.0}` are automatically normalized to ascending polynomial coefficients `{"c0": 1.0, "c1": 2.0}`.

---

## 3. AST Validation and Security Protections

To guarantee zero arbitrary code execution, user-submitted formulas are parsed using Python's `ast.parse(mode='eval')` and inspected by an AST visitor.

### Strict Whitelist Invariant

Only the following AST node types are permitted:
- `ast.Expression`
- `ast.BinOp` (with operators `Add`, `Sub`, `Mult`, `Div`, `Pow`)
- `ast.UnaryOp` (with operators `UAdd`, `USub`)
- `ast.Constant` (finite real numbers only; booleans, strings, and non-numeric types are rejected)
- `ast.Name` (whitelisted variable names: task input variable `x`, output variable, or declared parameter keys)

### Forbidden Constructs (Immediate Rejection)

The validator strictly prohibits:
- Dynamic execution: `eval()`, `exec()`, `compile()`, `breakpoint()`
- System/process calls: `os.*`, `sys.*`, `subprocess.*`, `shutil.*`, `open()`
- Dunder attributes / reflection: `__import__`, `__class__`, `__bases__`, `getattr`, `globals`, `locals`
- Function calls: `ast.Call` is completely forbidden (no function calls permitted)
- Attribute access: `ast.Attribute` is forbidden (no dot notation allowed)
- Data structure literals: lists `[]`, dictionaries `{}`, tuples `()`, sets
- Comprehensions and lambdas: `ast.Lambda`, `ast.ListComp`, etc.
- Boolean/comparison operations: `<`, `>`, `==`, `and`, `or`, `not`
- Negative or non-integer polynomial exponents
- Exponents exceeding hard maximum polynomial degree (12)
- Division by literal zero

### Structured Validation Error Codes

When an expression is malformed or violates security boundaries, a structured error is returned:

| Error Code | HTTP Status | Meaning |
| :--- | :--- | :--- |
| `UNSAFE_EXPRESSION` | 422 | Attempted code execution, dynamic imports, dunder attributes, or calls |
| `MALFORMED_EXPRESSION` | 422 | Syntax error in formula |
| `INVALID_EXPRESSION` | 422 | Empty formula, disallowed construct, or division by zero |
| `INVALID_DEGREE` | 422 | Polynomial degree exceeds hard ceiling (12) |
| `INVALID_PARAMETERS` | 422 | Non-numeric or non-finite parameter coefficients |
| `UNSUPPORTED_MODEL_FAMILY` | 422 | Requested model family is not registered |

---

## 4. Lifecycle Actions and API Endpoints

### 1. Propose Researcher Hypothesis

- **Endpoint**: `POST /api/v1/runs/{run_id}/hypotheses`
- **Payload**:
  ```json
  {
    "model_family": "polynomial",
    "formula": "y = 2*x + 1",
    "parameters": {"c0": 1.0, "c1": 2.0},
    "complexity": 2,
    "hypothesis_id": "hyp-optional-id"
  }
  ```
- **Behavior**:
  - Validates expression and parameters via safe AST parser.
  - Assigns a stable hypothesis ID (`hyp-...`).
  - Creates Revision 1 with `creation_source: "researcher"`.
  - Records a `HYPOTHESIS_PROPOSED` audit event.
  - Returns `201 Created` with serialized hypothesis.

### 2. Inspect Hypothesis and Evaluations

- **Endpoint**: `GET /api/v1/hypotheses/{hypothesis_id}`
  - Returns hypothesis state, flags (`is_pinned`, `is_excluded`), latest revision, and latest evaluation result.
  - **Oracle Invariant**: Hidden ground-truth formulas and oracle parameters are never exposed.
- **Endpoint**: `GET /api/v1/hypotheses/{hypothesis_id}/evaluations`
  - Returns the full chronological list of evaluation records for this hypothesis, ordered most-recent first.

### 3. Pin / Unpin Hypothesis

- **Endpoint**: `PATCH /api/v1/hypotheses/{hypothesis_id}`
- **Payload**: `{"is_pinned": true}` or `{"is_pinned": false}`
- **Behavior**:
  - Toggles the `is_pinned` display flag.
  - Records a `HYPOTHESIS_PINNED` or `HYPOTHESIS_UNPINNED` audit event.
  - Preserves scientific metric scores, formulas, and rank orders unchanged.

### 4. Exclude / Restore Hypothesis

- **Endpoint**: `PATCH /api/v1/hypotheses/{hypothesis_id}`
- **Payload**: `{"is_excluded": true}` or `{"is_excluded": false}`
- **Behavior**:
  - `is_excluded: true`: Marks candidate excluded, omitting it from `GET /api/v1/runs/{run_id}/hypotheses?include_excluded=false`.
  - `is_excluded: false`: Restores candidate, returning it to active candidate listing.
  - Records `HYPOTHESIS_EXCLUDED` or `HYPOTHESIS_RESTORED` audit event.
  - History, revisions, and evaluation records are fully preserved.

### 5. Re-evaluate Hypothesis

- **Endpoint**: `POST /api/v1/hypotheses/{hypothesis_id}/reevaluate`
- **Behavior**:
  - Checks if candidate is excluded: if `is_excluded: true`, returns `422 Unprocessable Entity` ("Cannot re-evaluate excluded hypothesis '{id}'. Restore it first before requesting re-evaluation.").
  - Evaluates latest revision against all current run observations via `EvaluationPipeline`.
  - Inserts a **new** `EvaluationResultModel` record with updated `dataset_version`, `sample_count`, and metrics ($MAE$, $RMSE$).
  - Records a `HYPOTHESIS_REEVALUATED` audit event.
  - If asynchronous worker is configured (`EVALUATION_DISPATCHER`), enqueues work and returns `202 Accepted`.

---

## 5. Concurrency and Optimistic Locking

To prevent race conditions and lost updates from concurrent researcher actions:
- Callers may pass `expected_revision` in `PATCH /api/v1/hypotheses/{id}`:
  ```json
  {
    "is_pinned": true,
    "expected_revision": 1
  }
  ```
- If the current revision of the hypothesis in the database does not match `expected_revision`, the request fails with `409 Conflict`:
  ```json
  {
    "error": "Conflict",
    "message": "Hypothesis 'hyp-123' revision conflict: expected revision 1, but current revision is 2.",
    "status_code": 409
  }
  ```
- Submitting a duplicate `hypothesis_id` returns `409 Conflict`.

---

## 6. Audit Trail and Traceability

All human-in-the-loop actions are permanently recorded in the `audit_events` table:

| Event Type | Actor | Metadata |
| :--- | :--- | :--- |
| `HYPOTHESIS_PROPOSED` | `researcher` | `hypothesis_id`, `revision_id`, `model_family`, `formula`, `complexity` |
| `HYPOTHESIS_PINNED` | `researcher` | `hypothesis_id`, `is_pinned: true` |
| `HYPOTHESIS_UNPINNED` | `researcher` | `hypothesis_id`, `is_pinned: false` |
| `HYPOTHESIS_EXCLUDED` | `researcher` | `hypothesis_id`, `is_excluded: true` |
| `HYPOTHESIS_RESTORED` | `researcher` | `hypothesis_id`, `is_excluded: false` |
| `HYPOTHESIS_REEVALUATED` | `researcher` | `hypothesis_id`, `revision_id`, `evaluation_id`, `status`, `sample_count` |
| `HYPOTHESIS_REEVALUATE_REQUESTED` | `researcher` | `hypothesis_id`, `revision_id`, `evaluation_id`, `dispatched: true` |
| `HYPOTHESIS_REEVALUATION_CONSUMED` | `worker` | `hypothesis_id`, `revision_id`, `evaluation_id`, `status`, `sample_count` |

---

## 7. Verification and Testing

Automated testing is maintained in `backend/tests/test_hypothesis_management.py`:
- `TestResearcherCreatedHypotheses`: Validates creation, stable ID generation, revision tracking, and audit logging.
- `TestExpressionSecurityAndValidation`: Parameterized testing of malicious code injection (`eval`, `exec`, `os.system`, `subprocess`, lambdas, reflection), malformed syntax, division by zero, and degree bounds.
- `TestPinningLifecycle`: Validates pinning/unpinning and verifies invariance of `HypothesisRanker` metric scores and rankings.
- `TestExclusionAndRestoration`: Validates active candidate filtering (`include_excluded=false`), history preservation (`include_excluded=true`), and restoration.
- `TestReevaluationAndHistory`: Validates re-evaluation creating new evaluation records, updating sample count/dataset version, preserving historical evaluations, and rejecting re-evaluation of excluded hypotheses.
- `TestConcurrencyAndConflicts`: Validates optimistic locking via `expected_revision` and duplicate ID collision rejection.
