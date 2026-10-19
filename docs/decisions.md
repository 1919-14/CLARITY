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

## Open decisions

1. First hypothesis representation/model family.
2. First deterministic oracle input domain/output type.
3. Candidate fitting objective, ranking formula, complexity proxy.
4. Disagreement and uncertainty score.
5. Nodus SDK version, task API, native budget controls, cost metadata, retry/cancellation semantics, concurrency limits.
6. MySQL library/ORM and migration tool.
7. Exact run states/transitions, cancellation, recovery.
8. Required report export formats.
9. Deployment target and clean-start procedure.
10. Target accuracy/error threshold and trial count for query-efficiency benchmarks.

For each resolved item, record status, decision, context, alternatives, trade-offs, validation/evidence, and date. Do not turn unverified assumptions into confirmed provider or scientific capabilities.
