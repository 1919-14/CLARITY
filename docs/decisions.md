# CLARITY Architecture Decision Record

Each entry is marked **Confirmed**, **Provisional**, or **Open**. Provisional directions guide the MVP but must be validated before becoming hard dependencies.

## D-001 — Flask, React, and MySQL

**Status:** Confirmed

**Decision:** Use Python/Flask for the backend/API, React for the researcher interface, and MySQL for persistence.

**Rationale:** Separates application/scientific services, user workflow, and durable run/evidence records.

**Trade-off:** More integration work than a single-process prototype; API and schema contracts must be agreed early.

## D-002 — Nodus Compute for remote parallel evaluation

**Status:** Confirmed direction; SDK details open

**Decision:** Nodus Compute is the intended remote execution platform for parallel hypothesis/experiment evaluation.

**Rationale:** Genuine platform usage is a core challenge requirement.

**Trade-off:** SDK, task semantics, concurrency, cost metadata, and provider access may constrain implementation.

**Validation:** Inspect actual SDK/environment, create a narrow adapter, and capture a real remote smoke test when access is available. Label mocked tests clearly.

## D-003 — Isolate hidden oracle truth

**Status:** Confirmed

**Decision:** Discovery receives permitted inputs/observations only. Hidden rules and evaluator-only labels are accessible only to the isolated evaluation harness.

**Rationale:** Leakage invalidates scientific claims.

**Trade-off:** Requires separate domain objects, serializers, fixtures, and benchmark interfaces.

**Validation:** Test that evaluator-only fields cannot leak into discovery objects, API responses, logs, or reports.

## D-004 — Active experimentation with competing hypotheses

**Status:** Confirmed

**Decision:** Maintain multiple candidate explanations and select informative experiments using disagreement and defensible uncertainty signals.

**Rationale:** The central thesis is improved use of a limited query budget versus random/passive sampling.

**Open details:** Hypothesis grammar/model families, scoring formula, uncertainty estimator.

## D-005 — Separate per-run limits from provider account balance

**Status:** Confirmed

**Decision:** Track per-run limits independently from account credits. Distinguish estimated, live/accruing, and settled/provider-reported costs.

**Rationale:** Application policy and provider billing are different concepts.

**Trade-off:** Requires concurrency-safe application checks; use provider-native controls only if verified.

## D-006 — Preserve researcher actions and scientific history

**Status:** Confirmed

**Decision:** Pinning is preference, not validation. Exclusion is reversible and preserves history. Re-evaluation preserves prior results where needed.

**Rationale:** Researcher input must not falsify scores or destroy provenance.

## D-007 — REST and polling first

**Status:** Provisional

**Decision:** Start with Flask REST and frontend polling; add SSE/WebSockets only when a demonstrated need justifies the complexity.

**Rationale:** Simpler to test and integrate under a tight deadline.

**Trade-off:** Polling can be less responsive and create repeated requests.

## D-008 — Active versus random benchmark

**Status:** Confirmed

**Decision:** Compare active selection with random/passive selection under comparable conditions and explicit query budgets.

**Rationale:** The project thesis must be evaluated empirically.

**Open details:** First oracle domain, target threshold, trial count, exact metric set.

## D-009 — Evidence-backed reports

**Status:** Confirmed

**Decision:** Report a ranked set of candidates (target 3–5 where available), metrics/evidence, usage, stop reason, and limitations. Missing metrics remain unavailable, not zero.

**Rationale:** A ranked candidate is not proof of truth.

## D-010 — Deterministic local oracle for early tests

**Status:** Provisional

**Decision:** Build the first vertical slice around a deterministic local oracle fixture.

**Rationale:** Enables repeatable end-to-end tests without external dependencies.

**Trade-off:** A fixture alone does not prove behavior on arbitrary real systems.

**Open detail:** Initial input domain and output type.

## D-011 — Keep the MVP focused

**Status:** Confirmed

**Decision:** Prefer small testable modules and the agreed stack; add infrastructure/streaming only with a clear rationale.

**Rationale:** The team has five preparation days before a 24-hour event.

**Trade-off:** Simpler initial implementations are acceptable, but agreed MVP scope must not be silently removed.

## D-012 — Human verification of AI-generated work

**Status:** Confirmed

**Decision:** AI-generated code must be reviewed, tested, and explainable by its owner. Do not invent provider APIs, benchmark results, test outcomes, or implementation status.

**Rationale:** Explicit verification reduces untested assumptions and late integration failures.

## D-013 — Bounded polynomial family is the first hypothesis family

**Status:** Confirmed (Issue #5)

**Decision:** One registered `polynomial` family covers constant, linear, and higher-degree models (degrees 0 through a configurable maximum, default 3, hard ceiling 12). Specifications use ascending coefficients `c0..c_degree`, require a non-zero leading coefficient for degree >= 1, and define complexity as `degree + 1`. Prediction is an explicit Horner evaluation over validated numeric coefficients.

**Rationale:** The family is small, testable, analytically checkable, and matches the deterministic scalar `x -> y` local oracle. A registry keeps further families addable without changing candidate generation or the discovery loop.

**Trade-off:** Narrower than unrestricted symbolic discovery. Higher-degree models with zero leading terms are rejected rather than silently treated as lower degree, so the declared degree and complexity stay honest.

**Rejected alternatives:** Expression strings with `eval`/`exec` or an expression parser (unsafe and unverifiable); arbitrary user code; separate constant/linear/quadratic classes (duplicate logic).

**Validation:** `backend/tests/test_hypotheses.py`; contract details in [contracts.md](contracts.md) section 4.

## D-014 — Generated candidates are initialized, not fitted

**Status:** Confirmed (Issue #5)

**Decision:** Candidate generation produces one deterministic monomial baseline per allowed degree plus seeded uniform coefficient draws until the requested unique count is reached. Candidates carry `status = candidate`, `system_generated` provenance, and generation metadata (generator, version, seed, configuration, initialization, sequence index). Duplicates are canonical-specification equality within one batch. An unreachable requested count raises `ATTEMPT_BUDGET_EXHAUSTED`, which reports bounded-sampling failure rather than a proven empty candidate space.

**Rationale:** Generated candidates need valid, explicit parameters to make predictions, but fitting belongs to Issue #6. Seeded initialization makes batches reproducible without implying fit quality.

**Trade-off:** Initial coefficients carry no evidence about the hidden system; generation must never be read as accuracy, confidence, or ranking. Duplicate detection covers canonical equality only, not general symbolic equivalence.

**Validation:** `backend/tests/test_hypotheses.py` (reproducibility, limits, exhaustion, isolation); [contracts.md](contracts.md) section 4.

## D-015 — Ordinary least squares for polynomial fitting

**Status:** Confirmed (Issue #6)

**Decision:** Fit the polynomial family by ordinary least squares using a dependency-free Householder QR solve on the Vandermonde matrix, in a scaled input variable that is converted back to the original basis. Require at least `degree + 1` training observations, allow exactly determined fits with an explicit warning, and fail rank-deficient, degenerate, or non-finite fits with structured errors instead of reducing degree or inventing coefficients.

**Rationale:** Least squares is the defensible, deterministic objective for numeric polynomial candidates. QR avoids the conditioning loss of normal equations, and scaling keeps degrees up to the hard ceiling usable without adding numpy.

**Trade-off:** A fit is a best fit to the supplied training data, not proof of the hidden rule. Rank detection is relative and threshold-based (`rank_tolerance`), so borderline cases are documented rather than silently clamped. Multi-output and robust/weighted objectives are out of scope.

**Rejected alternatives:** Normal equations (worse conditioning), gradient methods (nondeterministic/extra configuration), silently reducing degree (dishonest complexity), and adding numpy solely for this solve.

**Validation:** `backend/tests/test_evaluation.py` (exact recovery, overdetermined fit, insufficient/rank-deficient/degenerate failures); [contracts.md](contracts.md) section 5.

## D-016 — Evaluation pipeline keeps training and held-out data separate

**Status:** Confirmed (Issue #6)

**Decision:** `fit_candidate` accepts only training samples, and `EvaluationPipeline` fits once, then predicts on training and held-out partitions with the same fitted specification. Held-out outputs are used only for metrics. Failed fits produce `status=failed` with no metrics and no fitted specification. Per-candidate failures are isolated by `evaluate_many`.

**Rationale:** Held-out data must test generalization, never influence parameters, degree, method, or selection. Standardized results with counts, exclusions, metric definitions, and dataset IDs make later ranking and reporting auditable.

**Trade-off:** No automatic train/held-out splitting, no ranking on held-out scores, and one scalar output variable per candidate. Partial metric availability is represented as absent, never zero.

**Validation:** Leakage invariants A/B/E and failure-isolation tests in `backend/tests/test_evaluation.py`.

## D-017 — The discovery engine coordinates; it does not reimplement science

**Status:** Confirmed (Issue #8)

**Decision:** Add `DiscoveryEngine` as a thin orchestrator over the existing components: `CandidateGenerator` (#5), `EvaluationPipeline` (#6), `HypothesisRanker` (#7), `CandidateExperimentGenerator` (#9), and `OracleInterface` (#4). The engine owns lifecycle, immutable run state, budget accounting, deduplication, history, and result assembly. Experiment selection sits behind a narrow `ExperimentPlanner` protocol with a `DeterministicOrderPlanner` default. Terminal precedence is deterministic: satisfied configured stopping criterion → budget exhausted → max iterations → no valid experiment, with component/oracle failures terminating immediately.

**Rationale:** One coordinator prevents duplicated fitting/ranking logic and keeps the loop testable with deterministic local components. A narrow planner protocol lets Issues #10–#11 land active selection without changing the orchestrator.

**Trade-off:** The default planner spends queries deterministically rather than informatively, so early query efficiency is not a benchmark claim. The engine evaluates all candidates on every refresh (no incremental evaluation cache); this is cheap for the supported candidate-count ceiling.

**Rejected alternatives:** Putting selection or ranking formulas in the orchestrator (duplicates #7/#10); a generic workflow framework with configurable stages (speculative for one loop); doing the run inside a Flask service or persistence layer (couples domain logic to infrastructure).

**Validation:** `backend/tests/test_discovery_loop.py`; [discovery-loop.md](discovery-loop.md); [contracts.md](contracts.md) section 7.

## D-018 — Query budget and observation commit semantics

**Status:** Confirmed (Issue #8)

**Decision:** Reserve one unit of `max_query_budget` immediately before every `oracle.query` call. Raised calls, non-success responses, and contract-invalid responses consume that unit; pre-invocation validation rejections, duplicate skips, and externally supplied observations do not. A successful observation is committed immediately. If evaluation/ranking then raises, the observation and the previous ranking snapshot are kept, `ranking_is_fresh` becomes false, and the run terminates as `component_failure`. No automatic oracle retries exist.

**Rationale:** Budget must be a hard ceiling on real oracle invocations, and valid scientific evidence must never be discarded because a downstream recomputation failed. Recording staleness is more honest than presenting an unchanged ranking as current.

**Trade-off:** A failed query still costs budget, so a flaky oracle reduces the useful experiment count. The loop is training-only: all committed observations fit and test the candidates, so held-out metrics are unavailable inside the loop.

**Rejected alternatives:** Refunding budget for failed calls (understates real usage); retrying inside the engine (spends unbudgeted queries, risks duplicate side effects); rolling back committed observations on ranking failure (destroys valid evidence); silently continuing after a component failure (reports a ranking that was never computed).

**Validation:** Budget-ceiling, failure, and partial-result tests in `backend/tests/test_discovery_loop.py`; [discovery-loop.md](discovery-loop.md) sections 5, 7, and 8.

## D-019 — Terminal runs are immutable; resume is the explicit reopen path

**Status:** Confirmed (Issue #8 review follow-up)

**Decision:** A finalized discovery run is immutable. `add_observation()` raises `RUN_NOT_ACTIVE` once a result has been produced, leaving observations, ranking, and query accounting untouched. Post-terminal updates use `resume(observations)`, which is allowed only for a non-failed terminal run: it validates the whole batch before mutating state, ingests without consuming oracle budget, refreshes evaluation/ranking, and continues the bounded loop without resetting budget or iteration counters. Failed runs raise `RUN_TERMINAL_FAILURE` and are not resumable. Metric-threshold stopping now requires the candidate to be `PLAUSIBLE` in a rankable comparability tier, so a low metric alone can never be reported as scientific success. Duplicate policy is split: `allow_duplicate_queries` governs oracle re-querying, while `allow_duplicate_external_observations` governs repeated externally supplied inputs.

**Rationale:** A terminal result must describe one coherent state. Silent mutation after termination produces a result whose status/reason no longer explain its data. An explicit resume keeps incremental evidence support while making the lifecycle transition auditable. Requiring policy-level acceptance for metric stopping prevents an unconfigured acceptance threshold from being bypassed by a numeric target.

**Trade-off:** Resume cannot recover a failed run; callers start a new run with the accumulated observations instead. Resume also cannot extend beyond the original budget or iteration bounds, so a budget-exhausted run can ingest evidence but cannot query again.

**Rejected alternatives:** Allowing `add_observation()` after finalization (incoherent terminal snapshots); resuming failed runs (state may be deliberately stale, e.g. preserved ranking); treating a low training error as acceptance without the ranking policy's threshold (bypasses plausibility/comparability).

**Validation:** `backend/tests/test_discovery_loop.py` (post-terminal rejection, resume identity/budget preservation, failed-run resume rejection, metric-threshold regression, duplicate-policy split); [discovery-loop.md](discovery-loop.md) sections 3, 4, 8; [contracts.md](contracts.md) section 7.

## D-020 — Run snapshots freeze observation payloads

**Status:** Confirmed (Issue #8 follow-up review)

**Decision:** Freeze nested observation payloads (`input_values`, `output_values`, `noise_metadata`, and error `details`) whenever the engine takes ownership of an observation, and return those frozen snapshots from `engine.observations` and `DiscoveryRunResult.observations`. Caller-supplied `Observation` objects are copied at commit, and `EvaluationResult` objects in a result are deep copies. `Observation.to_dict` and `StructuredError.to_dict` thaw frozen payloads so serialization stays JSON-compatible.

**Rationale:** Frozen dataclasses only protect their top-level fields. Without deep freezing, a caller could mutate `result.observations[0].output_values` after a run and silently rewrite evidence that the lifecycle contract declares immutable, bypassing `RUN_NOT_ACTIVE`.

**Trade-off:** Observation payloads are `Mapping` objects rather than plain dicts inside the engine and result APIs; consumers that need a plain dict use `to_dict()` or `dict(obs.input_values)`. Evaluation snapshots are independent copies rather than frozen mappings, so mutating a returned evaluation cannot affect the engine but can still alter that caller-local object.

**Rejected alternatives:** Returning defensive copies only from `engine.observations` (result snapshots would remain aliased); freezing every `Observation` at construction globally (broader contract change than this boundary requires); freezing `EvaluationResult` payloads (its serializer and downstream consumers currently assume plain dicts).

**Validation:** `backend/tests/test_discovery_loop.py::test_terminal_observation_snapshots_reject_nested_mutation`; [discovery-loop.md](discovery-loop.md) section 8; [contracts.md](contracts.md) section 7.5.

## Open decisions

1. ~~First hypothesis representation/model family.~~ Resolved by D-013 (polynomial family) and Issue #5's extended `HypothesisRepresentation`.
2. First deterministic oracle input domain/output type.
3. ~~Candidate fitting objective.~~ Resolved for the polynomial family by D-015. Ranking formula remains open; complexity proxy is `degree + 1` (D-013).
4. Disagreement and uncertainty score.
5. Nodus SDK version, task API, native budget controls, cost metadata, retry/cancellation semantics, concurrency limits.
6. MySQL library/ORM and migration tool.
7. ~~Exact run states/transitions, cancellation, recovery.~~ Resolved for the discovery loop by D-017/D-018/D-019; run-level cancellation and durable recovery remain open for the API/persistence layer.
8. Required report export formats.
9. Deployment target and clean-start procedure.
10. Target accuracy/error threshold and trial count for query-efficiency benchmarks.

For each resolved item, record status, decision, context, alternatives, trade-offs, validation/evidence, and date. Do not turn unverified assumptions into confirmed provider or scientific capabilities.
