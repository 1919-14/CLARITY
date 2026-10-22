# CLARITY Architecture Decision Record

Each entry is marked **Confirmed**, **Provisional**, or **Open**. Provisional directions guide the MVP but must be validated before becoming hard dependencies.

## D-001 — Flask, React, and MySQL

**Status:** Confirmed

**Decision:** Use Python/Flask for the backend/API, React for the researcher interface, and MySQL for persistence.

**Rationale:** Separates application/scientific services, user workflow, and durable run/evidence records.

**Trade-off:** More integration work than a single-process prototype; API and schema contracts must be agreed early.

## D-002 — Nodus Compute for remote parallel evaluation

**Status:** Confirmed

**Decision:** Nodus Compute is the remote execution platform for parallel
hypothesis/experiment evaluation. The adapter uses `nodus-compute==1.0.27` with
the following verified public API:

- `nodus.Function.from_name(app: str, name: str, project=None)` — function lookup
- `fn.spawn(**kwargs) -> FunctionCall` — submits the call; kwargs go to the remote function
- `handle.get(timeout=...) -> Any` — waits and returns the function's return value directly
- `handle.name: str` — provider FunctionCall identifier
- `nodus.errors.NodusError.retryable: bool` — True for `TooManyRequests` and `Unavailable`

**Rationale:** Genuine platform usage is a core challenge requirement. The adapter
boundary (`backend/app/adapters/nodus/`) isolates all SDK details from application
logic and enforces explicit execution modes with no fallback.

**Trade-off:** `handle.get()` returns the function's return value directly (not a
status envelope). The remote function's return dict becomes `result_data` verbatim.
Retries are pre-submission-only — post-submission retry would create duplicate
billable calls.

**Validation:** Adapter tested with 70 unit tests (including running event loops,
threadless contexts, native async execution, process-global client serialization
and concurrency regression tests across async tasks and threads, client lifecycle,
and mocked SDK boundary). Genuine remote smoke test (`python -m app.nodus_smoke_test`)
is ready; pending a deployed Function with matching `NODUS_APP_NAME`/`NODUS_FUNCTION_NAME`
and `NODUS_API_KEY`. See `docs/nodus-compute.md` for deployment and smoke-test
instructions.

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

**Decision:** Freeze nested observation payloads (`input_values`, `output_values`, `noise_metadata`, and error `details`) whenever the engine takes ownership of an observation, and return those frozen snapshots from `engine.observations` and `DiscoveryRunResult.observations`. Caller-supplied `Observation` objects are copied at commit. Returned `EvaluationResult` snapshots freeze their nested mappings too (`metrics`, `held_out_metrics`, `fitting_config`, `fitted_specification`, `observation_counts`, `exclusion_codes`, `metric_definitions`, `dataset_ids`, plus error and warning details). `Observation.to_dict`, `EvaluationResult.to_dict`, and `StructuredError.to_dict` thaw frozen payloads so serialization stays JSON-compatible.

**Rationale:** Frozen dataclasses only protect their top-level fields. Without deep freezing, a caller could mutate `result.observations[0].output_values` after a run and silently rewrite evidence that the lifecycle contract declares immutable, bypassing `RUN_NOT_ACTIVE`.

**Trade-off:** Observation and evaluation payloads are `Mapping` objects rather than plain dicts inside the engine and result APIs; consumers that need plain values use `to_dict()` or `dict(obs.input_values)`. Frozen evaluation snapshots still go through `dataclasses.replace` at result assembly, which is a small per-candidate cost paid only when a result snapshot is built.

**Rejected alternatives:** Returning defensive copies only from `engine.observations` (result snapshots would remain aliased); freezing every `Observation` at construction globally (broader contract change than this boundary requires); copying `EvaluationResult` objects without freezing their mappings (a returned result could still mutate its own serialization and break the documented immutability guarantee).

**Validation:** `backend/tests/test_discovery_loop.py::test_terminal_observation_snapshots_reject_nested_mutation`; [discovery-loop.md](discovery-loop.md) section 8; [contracts.md](contracts.md) section 7.5.

## D-021 — Disagreement-driven selection with an unavailable uncertainty signal

**Status:** Confirmed (Issue #10)

**Decision:** The first active selector scores each valid candidate by the spread of eligible hypotheses' finite scalar predictions and chooses the maximum. Population variance is the default metric (divides by `n`); sample variance (`n - 1`) and maximum spread are supported and named explicitly. Variance is computed from deviations centered on the first prediction, scaled by their own largest magnitude, and rescaled with `math.frexp`/`math.ldexp`; this avoids both `scale * scale` intermediate overflow and precision loss for small relative spreads at large magnitudes. A genuinely unrepresentable final score is reported as `DISAGREEMENT_OVERFLOW`, never as zero or infinity. Scores are never normalized across the pool, and an all-zero pool returns `ZERO_DISAGREEMENT_POOL` instead of manufacturing informativeness.

Eligibility uses ranking `CONTRADICTED`/`UNEVALUABLE` exclusion plus evaluation/fitted-specification availability. Fitted specifications are used whenever at least `min_eligible_hypotheses` valid fits exist. The hypotheses' own validated candidate specifications are used only through the explicit `allow_candidate_specifications` opt-in, and then for the whole decision; fitted and unfitted predictions are never mixed. Ties are broken by canonical input key then candidate ID, making the result independent of batch order. Previously queried inputs are excluded only when the candidate and observation have identical input variable names, every numeric value satisfies pure absolute tolerance, and boolean/string values match exactly; partially overlapping schemas are never treated as duplicates, with the engine's duplicate guard remaining the final safety net. A `random_baseline` strategy uses a local seeded `random.Random` over the same valid pool and never consults disagreement. Structured diagnostics are stored in `ExperimentExecutionRecord.selection_diagnostics` through an optional planner method, while `select_experiment(...) -> Optional[CandidateExperiment]` remains unchanged.

Uncertainty is reported unavailable. The fit/evaluation contracts expose no validated prediction covariance, residual variance, or interval, so no polynomial variance, covariance matrix, or calibrated probability is invented, and `uncertainty_weight` must remain `0.0`.

**Rationale:** Prediction disagreement is directly measurable from permitted discovery data, is deterministic, and has an interpretable formula. Population variance is the simplest documented convention and avoids the `n = 1` undefined case (a minimum of two hypotheses is enforced). Refusing to normalize an uninformative pool and refusing to invent uncertainty prevents a selection score from looking like evidence it is not. A backward-compatible planner adapter preserves every Issue #8 consumer while exposing reproducible decisions.

**Trade-off:** The score is output-scale sensitive and greedy over one generated batch; it is not information gain or success probability. Strict fitted mode requires at least two successful fits, so active runs need enough initial observations or an explicit candidate-specification opt-in. Training-only evaluation in the engine (Issue #8) means the fitted source reflects training fits only. The random baseline is a benchmark control, not a claim of inferiority or superiority.

**Rejected alternatives:** Normalizing scores per pool (turns zero disagreement into artificial spread); treating disagreement variance as calibrated uncertainty (unsupported by any fit contract); deriving polynomial prediction variance without covariance/residual metadata (would invent statistical assumptions); silently falling back to random or sequential selection (misreports the strategy); changing `select_experiment` to return a result object (breaks existing planners and callers); seeding from global `random` state (not reproducible or isolated).

**Validation:** `backend/tests/test_experiment_selection.py` (metrics including large-magnitude representable variance and unrepresentable overflow, discriminative selection, ties and order reversal, duplicate tolerance and schema compatibility, fallback, eligibility, strict/opt-in sources, seeded baseline, planner compatibility, engine integration, frozen diagnostics, ground-truth isolation); [experiment-selection.md](experiment-selection.md); [contracts.md](contracts.md) §8; [discovery-loop.md](discovery-loop.md) §6.

## D-022 — Optional, provider-independent LLM Scientist that only proposes

**Status:** Confirmed (Issue #41)

**Decision:** Add an opt-in LLM Scientist behind a narrow `LLMProposalProvider` protocol. It receives only a bounded, permitted evidence context (committed observations, candidate specifications, evaluation metrics, ranking summaries, prior round outcomes) and returns structured proposals that are parsed as untrusted JSON, schema/domain validated, canonicalized, deduplicated, and converted into the existing `HypothesisRepresentation` contract. Groq is the first runtime provider behind that protocol. Deterministic-only remains the default and requires no credentials, network, or provider package at import time.

The engine calls at most one proposal round after the initial ranking pass and after each successful query, bounded by `LLMScientistConfig.max_rounds`; rounds consume no oracle budget. Hypotheses enter the existing fitting/evaluation/ranking pipeline with stable IDs and provenance (provider, model, prompt/schema versions, round ID, uncalibrated self-confidence, live-vs-fake flag). Advisory experiment suggestions are queued for the existing `CandidateExperimentGenerator` through `ExperimentGenerationConfig.suggested_inputs`, so the planner, oracle validation, duplicate guard, query budget, and stopping policy remain authoritative.

Provider failures and empty rounds emit `SCIENTIST_FALLBACK`, record a non-success round in `DiscoveryRunResult.scientist_history`, and continue deterministic discovery; `disable_further_rounds` additionally stops provider calls for the run. Evaluator-only ground truth is never serialized into requests, records, logs, or results, and both request/record construction fail closed on forbidden keys.

**Rationale:** The LLM can enrich the hypothesis space without becoming a scientific authority. A provider-independent interface keeps the discovery engine free of SDK details and leaves room for another compatible provider. Bounded context/response/round/retry limits and strict local validation prevent unbounded token, candidate, or query growth. Explicit fallback records prevent a provider failure from masquerading as an LLM success.

**Trade-off:** Proposal quality depends on provider, model, and prompt, and is not reproducible at non-zero temperature. Only the one-dimensional numeric polynomial family is supported; other structures are rejected rather than approximated. Confidence is uncalibrated and never used. The offline synthetic benchmark showed no held-out error advantage because the deterministic generator already contains a cubic candidate that fits the noiseless synthetic oracle; no improvement is claimed.

**Rejected alternatives:** Letting the LLM decide correctness or write rankings (violates the proposal/validation split); executing generated code or expressions (unsafe and unsupported); exposing a generic serializer of discovery objects (leaks evaluator/private state); automatic silent fallback to generated candidates labeled as LLM output (misreports provenance); adding a parallel experiment planner or oracle path (bypasses Issue #9/#10/#11 controls); requiring a live provider for the test suite (non-reproducible, credential-dependent).

**Validation:** `backend/tests/test_llm_scientist.py` (schema/domain/safety rejection, canonical dedupe, provider outcome taxonomy, bounded retries, secret hygiene, deterministic mode, pipeline integration, evidence feedback, fallback, suggestion routing, state immutability, isolation, reproducibility); `backend/benchmarks/llm_scientist_benchmark.py`; [llm-scientist.md](llm-scientist.md); [contracts.md](contracts.md) §9.

## D-023 — Independent candidate evaluation runs in parallel through Nodus

**Status:** Confirmed (Issue #13); live remote verification pending

**Decision:** Candidate evaluations are orchestrated as independent tasks by
`ParallelEvaluationRunner` (`backend/app/services/parallel_evaluation.py`) over
the Issue #12 adapter. `NODUS_PARALLEL_EXECUTION_MODE` selects
`local_sequential` or `nodus_parallel` explicitly and has no default.
`nodus_parallel` requires the adapter to be in `nodus_remote` mode; any
configuration, credential, deployment, or provider failure is reported as a
structured failure and never falls back to local/mock execution. Both modes run
the same serializable worker function
(`services/evaluation_task_runtime.execute_evaluation_task_payload`), which
reuses `EvaluationPipeline` without changing fitting or metrics.

Adapter concurrency is a bounded thread pool inside one client scope.
`NodusExecutionAdapter.execute_many` acquires the process-global client lock
once, configures the SDK default client once, runs up to `max_concurrency`
tasks, and restores the client once. The orchestrator owns retries (adapter
`max_retries=0`) and retries only failures with no provider task ID and an
explicitly retryable code. Aggregation is separate from submission and orders
results by original request order, recording missing, duplicate, unexpected,
and mismatched results explicitly. Failed or timed-out tasks produce no
`EvaluationResult`; the discovery-facing pipeline maps them to
`EvaluationResult(status=FAILED)` with no metrics.

**Rationale:** Candidate evaluations are independent and dominated by fit/predict
work, so they are the natural unit of parallelism. Reusing one payload/worker
path in both modes makes sequential-versus-parallel comparison meaningful and
gives the discovery loop an unchanged `evaluate_many` boundary. Refusing to fall
back keeps the reported execution mode truthful.

**Trade-off:** The pinned SDK exposes blocking wrappers and one process-global
default client, so real concurrency uses threads and one batch-wide lock rather
than `asyncio.gather`; a slow task occupies a thread for its duration. Retries
before acceptance can re-run a call whose outcome was ambiguous, which is why
post-acceptance failures (provider task ID present) are never retried. Local
mode is sequential by design, so it is a correctness baseline, not a
throughput baseline.

**Rejected alternatives:** Silent fallback to local evaluation when remote is
unavailable (misreports the execution mode); retrying after a handle exists
(duplicate billable work); per-task client creation (concurrent swaps of the
SDK's single default client); using the SDK's `.map()` fan-out (no per-call
provider IDs for correlation); rewriting the discovery loop to be parallel-aware
(unnecessary coupling); adding a queue/broker for one bounded batch.

**Validation:** `backend/tests/test_nodus_parallel.py` (76 offline tests:
configuration, payload/result round trips, identity, deterministic ordering,
concurrency limits 1..4, failures/timeouts/malformed/missing/duplicate/
unexpected results, bounded retries, cancellation, adapter batch scope,
discovery integration, oracle non-duplication, strict JSON, secret hygiene);
Issue #12 adapter suite (70 tests) and the full backend suite remain green.
**Live remote smoke test and remote benchmark have not run — no Nodus credits or
service access.** See [nodus-parallel.md](nodus-parallel.md) and
[nodus-readiness.md](nodus-readiness.md).

## Open decisions

1. ~~First hypothesis representation/model family.~~ Resolved by D-013 (polynomial family) and Issue #5's extended `HypothesisRepresentation`.
2. First deterministic oracle input domain/output type.
3. ~~Candidate fitting objective.~~ Resolved for the polynomial family by D-015. Ranking formula remains open; complexity proxy is `degree + 1` (D-013).
4. ~~Disagreement and uncertainty score.~~ Resolved for the first implementation by D-021 (population variance default, sample variance/max spread supported, uncertainty unavailable).
5. ~~Nodus SDK version, task API, native budget controls, cost metadata, retry/cancellation semantics, concurrency limits.~~ Resolved by D-002 (SDK surface, pre-submission retry policy) and D-023 (batch concurrency, thread-based worker pool). Native per-call cost metadata and confirmed remote cancellation remain unavailable; live concurrency behaviour is pending verification.
6. MySQL library/ORM and migration tool.
7. ~~Exact run states/transitions, cancellation, recovery.~~ Resolved for the discovery loop by D-017/D-018/D-019; run-level cancellation and durable recovery remain open for the API/persistence layer.
8. Required report export formats.
9. Deployment target and clean-start procedure.
10. Target accuracy/error threshold and trial count for query-efficiency benchmarks.

For each resolved item, record status, decision, context, alternatives, trade-offs, validation/evidence, and date. Do not turn unverified assumptions into confirmed provider or scientific capabilities.
