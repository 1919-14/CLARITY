# CLARITY Architecture

## Status and goals

This is the intended MVP architecture, not a claim that components are already implemented. Preserve clear boundaries between discovery logic, UI, persistence, oracle access, and provider-specific execution. Keep hidden truth away from the discovery engine, enforce limits before scheduling work, and make the system testable without live services for every unit test.

The concrete field definitions, validation rules, structured errors, and serialization guardrails for oracle, experiment, observation, hypothesis, evaluation, and benchmark data live in [contracts.md](contracts.md).

## Logical architecture

```mermaid
flowchart TD
    R[Researcher] --> UI[React Research UI]
    UI --> API[Flask REST API]
    API --> RUN[Run Lifecycle / Orchestrator]
    API <--> DB[(MySQL)]
    RUN --> BUD[Budget and Usage Manager]
    RUN --> DISC[Discovery Engine]
    DISC --> HYP[Hypothesis Generator / Fitter / Ranker]
    DISC --> PLAN[Experiment Generator / Active Selector]
    PLAN --> CHECK[Validation and Budget Gate]
    CHECK --> ORACLE[Oracle Adapter]
    ORACLE --> OBS[Observation]
    OBS --> DISC
    DISC --> EXEC[Nodus Execution Adapter]
    EXEC --> NODUS[Nodus Compute]
    EXEC --> DISC
    RUN --> REPORT[Report and Metrics]
    REPORT --> UI
    EVAL[Isolated Evaluator] --> MET[Ground-truth Metrics]
    MET --> REPORT
```

The diagram is conceptual. Modules may be grouped differently, but the responsibilities and data-isolation guarantees must remain.

## Component responsibilities

### React research interface
- Configure runs and display status, progress, query counts, limits, and stop reason.
- Browse observations and hypotheses; show measured metrics, ranking rationale, evidence, and uncertainty only when supported.
- Submit researcher actions: add, pin/unpin, exclude/restore, and re-evaluate.
- Display reports and benchmark comparisons, including missing values and caveats.
- Provide loading, empty, unavailable, and error states.

The frontend is not authoritative for budgets or scientific scores. Server-side validation and enforcement are required.

### Flask API and services
- Validate requests and return consistent errors.
- Expose health, run, hypothesis, observation, report, and metric resources.
- Coordinate run lifecycle and researcher actions.
- Call domain services and persistence through stable interfaces.
- Keep scientific algorithms out of route handlers.
- Never expose hidden ground truth through ordinary API responses.

Confirmed REST API Endpoints (implemented in Issue #19, detailed in [docs/api.md](api.md)):
- `GET /api/health`
- `POST /api/runs`
- `GET /api/runs`
- `GET /api/runs/{run_id}`
- `POST /api/runs/{run_id}/cancel`
- `GET /api/runs/{run_id}/hypotheses`
- `POST /api/runs/{run_id}/hypotheses`
- `PATCH /api/runs/{run_id}/hypotheses/{hypothesis_id}`
- `GET /api/runs/{run_id}/observations`
- `GET /api/runs/{run_id}/metrics`
- `GET /api/runs/{run_id}/report`
- `GET /api/runs/{run_id}/benchmarks`

### Run lifecycle and orchestrator
Creates runs, initializes the engine, coordinates evaluation → selection → oracle query → observation persistence → update, checks budgets before work, tracks status/progress, and records terminal reason. States must distinguish successful completion, budget/limit stopping, cancellation, and failure. Preserve successful partial results when independent tasks fail. Use REST polling first; add streaming/workers only when needed and documented.

### Discovery engine
Keep domain logic independent of Flask request objects, React state, MySQL-specific models, and Nodus SDK internals. Subcomponents:
- **Hypothesis generator:** candidates in the supported family.
- **Fitter/evaluator:** fit candidates and calculate defined metrics from permitted observations.
- **Ranker/evidence tracker:** reproducible ranking and provenance.
- **Experiment generator:** valid candidate inputs.
- **Active selector:** disagreement and defensible uncertainty/information signals.
- **Stopping policy:** continue or stop with explicit reason.
- **Step interface:** execute a discovery step and return explicit outcomes.
- **Optional LLM Scientist (#41):** a disabled-by-default, provider-independent proposal source. It consumes only permitted observations/evidence and returns validated proposals through the existing hypothesis contract; CLARITY still owns fitting, evaluation, ranking, selection, oracle access, budget, and stopping. See [llm-scientist.md](llm-scientist.md).

Issues #5-#7 and #9 implement the scientific subcomponents. Issue #8 adds the coordinator, `DiscoveryEngine` in `backend/app/domain/discovery_engine.py`, which connects them through their public interfaces only: candidate generation, one `evaluate_many` refresh, one `rank_candidates` call, experiment generation, a narrow `ExperimentPlanner` selection boundary, and budget-checked `OracleInterface` queries. The engine owns the deterministic termination precedence, query accounting, observation commit boundary, run history, and the immutable `DiscoveryRunResult`. Active selection (#10/#11) plugs in as a planner without changing the orchestrator. See [discovery-loop.md](discovery-loop.md).

Prioritize a narrow deterministic benchmark over a broad, unverifiable model library.

### Oracle adapter
Accepts a validated query and returns an observation. It must not expose rule parameters or evaluator labels to the discovery engine. Support deterministic local fixtures and keep the interface replaceable. Retries must be bounded and reflected in query accounting.

### Nodus execution adapter
Hides provider SDK details, submits independent tasks where supported, maps results/errors to task and candidate IDs, and handles timeouts, partial failures, bounded retries, and cancellation where available. It must not silently fall back to a local mock when remote mode was requested.

Mocked tests prove behavior against a mock only. Genuine remote-execution claims require a real task and redacted provider evidence. Verify SDK methods, native budget controls, cost reporting, and concurrency behavior before relying on them.

### Budget and usage services
Separate provider account balance from per-run limits. Run limits may include oracle queries, evaluation tasks, concurrency, wall-clock time, and a spend ceiling where supported. Enforce application-side limits before scheduling, including concurrent work. Track estimated, live/accruing, and settled/provider-confirmed costs separately. Unknown values remain unavailable.

### MySQL persistence
Persist runs/configuration, observations, hypotheses and revisions, evaluation results, experiment selections where useful, budget/usage records, audit events, and benchmark definitions/results. Use relationships and transactions appropriately. Preserve meaningful history. Table names, ORM, migration tooling, and indexes are implementation choices.

### Reporting and metrics
Build reports from persisted evidence. Show the top 3–5 candidates where available, representation, fit/held-out error, defined complexity, evidence, defensible uncertainty, usage, stop reason, and limitations. Missing metrics are unavailable, not zero. Record metric definitions and benchmark versions. MAE and RMSE definitions, aggregation, and failure semantics are specified in [contracts.md](contracts.md) section 5.

### Evaluator and benchmark runner
The evaluator is separate from discovery. It may access hidden rules/labels to calculate benchmark metrics, but these must not enter agent-facing inputs or ordinary discovery API responses. Compare active selection and random/passive selection under equivalent task/oracle family, candidate procedure, query budget, held-out evaluation, and documented seeds/repeated trials.

## End-to-end data flow

1. Researcher submits run configuration through React.
2. Flask validates it, persists the run, and returns an ID/status.
3. Orchestrator initializes the engine with permitted data only.
4. Engine generates candidate hypotheses.
5. Candidates are evaluated locally or through Nodus, according to the selected mode.
6. Results are associated with candidate IDs and persisted.
7. Ranker produces candidates with measured metrics and evidence.
8. Experiment generator proposes valid candidate queries.
9. Active selector scores queries using disagreement/uncertainty signals.
10. Optionally, the LLM Scientist proposes new candidate hypotheses from the
    newly committed evidence; proposals are validated locally and re-enter the
    same fitting/evaluation/ranking pipeline.
11. Budget manager checks whether more oracle and compute work is permitted.
12. Oracle adapter executes the selected query and returns an observation.
13. Observation and run event are persisted; candidate evaluations/evidence update.
14. Repeat until a stopping condition or resource limit is reached.
15. Record terminal status/reason and build the report from persisted evidence.
16. Frontend polls and displays hypotheses, observations, report, and metrics.
17. Separately, evaluator uses ground truth for benchmark scoring without exposing answers to discovery.

## Data isolation and failure rules

- Fitting consumes training observations only. Held-out observations are used for metrics after the fit and must never reach the fitter, determine the fitting method, or influence degree selection (Issue #6; see [contracts.md](contracts.md) section 5).
- Keep evaluator-only fields out of discovery objects and serializers.
- Never put secret rules in shared configuration, logs, debug messages, frontend bundles, or reports.
- Use explicit API serialization allowlists.
- Test that ground truth cannot leak into agent-facing objects or responses.
- A failed task is not a zero-valued metric; unknown results remain unavailable.
- Preserve completed observations/evaluations after budget exhaustion.
- Record real vs mocked remote execution accurately.
- Bound retries and prevent duplicate events from creating duplicate records where idempotency is required.
- The discovery loop reserves query budget before every oracle call, never commits an invalid oracle response, and marks `ranking_is_fresh = false` instead of presenting a stale ranking as current ([discovery-loop.md](discovery-loop.md)).
- The optional LLM Scientist receives only permitted evidence, cannot execute generated code, call an oracle, or mutate committed state, and records every round (including failures and fallbacks) without credentials ([llm-scientist.md](llm-scientist.md)).

## Suggested code boundaries

```text
backend/
  app/
    api/             # Flask routes and request/response schemas
    services/        # orchestration, budgets, reports, benchmarks
    domain/          # hypotheses, observations, experiments, policies
    adapters/
      oracle/        # black-box query adapters
      llm/           # optional LLM Scientist provider adapters (#41)
      nodus/         # remote execution adapter
      persistence/   # MySQL/repository implementations
    config.py
  tests/
    unit/
    integration/
```

This is a suggestion, not a mandatory layout before the application skeleton exists.

## Verification strategy

- **Unit:** contracts, validation, fitting/ranking, experiment scoring, stopping, budget gates, serializers, state transitions.
- **Deterministic integration:** local oracle fixture and fake execution adapter.
- **Database:** persistence, transactions, migrations, revision history.
- **Nodus adapter:** mocked success, partial failure, timeout, malformed payload.
- **Real Nodus smoke test:** separately recorded when access is available.
- **Frontend:** run setup, polling, researcher actions, error states, reports/benchmarks.
- **Scientific:** active vs random under matched budgets.
- **End-to-end:** clean startup, discovery flow, persisted evidence, budget behavior, report, measured benchmark, limitations.

See [decisions.md](decisions.md) for unresolved architecture choices.
