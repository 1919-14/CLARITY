# CLARITY — Master Project Context

> **Purpose:** Portable project briefing for teammates, reviewers, and AI coding agents. Read this before starting an issue; it captures project intent without relying on private chat history.
>
> **Status rule:** This describes intended MVP scope, not proof that a feature is implemented. Inspect code, tests, provider evidence, and the assigned issue before making implementation claims.

## 1. Project identity and goal

- **Name:** CLARITY — Autonomous Black-Box Scientist
- **Repository:** https://github.com/1919-14/CLARITY
- **Hackathon:** Hackyard Build 2026 by IIT Guwahati, including the Nodus Compute Challenge.
- **Team:** Four contributors.
- **Preparation:** Five days before the official 24-hour onsite event. Aim to have an end-to-end MVP before the event; reserve the event for integration, debugging, validation, and demo rehearsal.

CLARITY aims to discover unknown system behavior through active experimentation. The core thesis is that maintaining competing explanations and choosing observations that distinguish them can use a limited query budget more effectively than random/passive sampling.

## 2. Problem and scientific model

A researcher can submit inputs to a black-box system and observe outputs, but cannot inspect its internal rule. The challenge is choosing the next experiment. CLARITY should:
1. Maintain multiple plausible hypotheses.
2. Evaluate candidates against permitted observations using reproducible metrics.
3. Identify disagreement or uncertainty only where it can be estimated defensibly.
4. Generate valid experiments that distinguish candidate explanations.
5. Enforce query and compute limits before scheduling work.
6. Query the oracle and incorporate the returned observation.
7. Update candidate evaluations, evidence, and rankings.
8. Stop honestly when a budget or documented stopping condition is reached.
9. Report ranked candidates, evidence, metrics, ambiguity, and limitations.
10. Compare active selection with a random/passive baseline under comparable budgets.

The correct explanation may not be in the candidate set. A first-ranked candidate is not automatically true. An unsuccessful but honestly reported run is preferable to fabricated success.

### Oracle, discovery engine, evaluator

- **Oracle:** accepts a valid query and returns an observation. Its internal rule/parameters remain hidden from the discovery engine.
- **Discovery engine:** receives only permitted inputs and observations. It generates/evaluates hypotheses and chooses experiments.
- **Evaluator:** may access hidden ground truth for controlled benchmarking, but this information must not leak into agent-facing objects, ordinary API responses, frontend bundles, logs, or reports.

Use explicit interfaces and separate data objects for these responsibilities. Deterministic local oracle fixtures should support early reproducible tests.

## 3. Intended MVP scope

1. Oracle and data contracts for inputs, observations, hypotheses, evaluations, run configuration, and benchmark results.
2. Candidate hypothesis generation and representation.
3. Candidate fitting/evaluation and predictive metrics.
4. Transparent ranking and evidence tracking.
5. End-to-end discovery orchestrator.
6. Valid candidate experiment generation.
7. Disagreement/uncertainty-driven experiment selection.
8. Input validation, query limits, and stopping conditions.
9. Nodus remote execution adapter with real task/result identity and explicit errors.
10. Parallel evaluation where supported and useful.
11. Per-run query/evaluation, concurrency, runtime, and spend limits where measurable/supported.
12. Usage records distinguishing estimated, live/accruing, and settled/provider-confirmed cost.
13. Researcher controls: add, pin/unpin, exclude/restore, re-evaluate.
14. Audit events and history for researcher actions and hypothesis revisions.
15. MySQL persistence for runs, observations, hypotheses, evaluations, usage, and events.
16. Flask REST API for health, runs, hypotheses, observations, metrics, and reports.
17. Explicit run lifecycle, progress, stop reasons, errors, and partial-failure handling.
18. React researcher dashboard and hypothesis/evidence interface.
19. Ranked scientific report/export, targeting top 3–5 candidates where available.
20. Ground-truth evaluation using Hit@K/MRR where appropriate and MAE/RMSE for suitable numeric prediction tasks.
21. Reproducible active-versus-random benchmark under matched conditions.
22. Measured accuracy/quality, query efficiency, runtime, cost where measurable, and complexity.
23. Dedicated Agent Performance & Scientific Benchmarks page.
24. Robustness tests for noise, ambiguity, malformed inputs, failures, timeouts, budget exhaustion, and incomplete results.
25. Clean setup, deployment, end-to-end demo, and evidence-backed rehearsal.

Do not silently drop agreed scope. If time/provider constraints make something infeasible, document the limitation and obtain an explicit decision.

## 4. Stack and target API

| Component | Direction |
|---|---|
| Frontend | React |
| Backend/API | Python + Flask |
| Persistence | MySQL |
| Remote compute | Nodus Compute SDK/service, after SDK behavior is verified |
| Initial status transport | REST + polling |
| Tests/CI | Unit/integration tests, frontend checks/build, GitHub Actions |

Target endpoint sketch (not proof of implementation or a frozen contract):
- `GET /api/health`
- `POST /api/runs`
- `GET /api/runs/{run_id}`
- `GET /api/runs/{run_id}/hypotheses`
- `POST /api/runs/{run_id}/hypotheses`
- `PATCH /api/runs/{run_id}/hypotheses/{hypothesis_id}`
- `GET /api/runs/{run_id}/observations`
- `GET /api/runs/{run_id}/report`
- `GET /api/runs/{run_id}/metrics`

Finalize schemas/status codes before parallel frontend/backend implementation. Cancellation, individual researcher actions, and benchmark retrieval may need dedicated endpoints.

## 5. Budget semantics

Keep these concepts separate:
- **Provider account balance:** account credits/balance; never invent or assume it.
- **Per-run budget:** limits configured for one discovery run.
- **Oracle query count:** actual calls under a documented counting policy.
- **Evaluation/task count:** scheduled/executed compute work, which may differ from oracle calls.
- **Estimated cost:** calculated estimate with assumptions/units.
- **Live/accruing usage:** provider-reported or observed usage that may not be final.
- **Settled cost:** final provider-reported charge when available.

Check limits before scheduling work, including concurrency and retries. Claim provider-native enforcement only if verified and enabled. Preserve completed results if later work fails or the budget is exhausted.

## 6. Metrics and benchmark fairness

- **Hit@K:** correct evaluator-identified candidate appears in the top K.
- **MRR:** reciprocal rank of the correct candidate when a meaningful target exists.
- **MAE/RMSE:** held-out numeric prediction error where appropriate.
- **Queries-to-target-accuracy:** query count to reach a predeclared threshold; define threshold and failure behavior.
- **Runtime/cost:** measured wall-clock/provider usage, with conditions and limitations.
- **Complexity:** defined reproducible proxy, not a subjective unexplained score.

Precision@K, Recall@K, and NDCG@K require meaningful relevance labels. Active and random baselines must use comparable oracle/task family, query budgets, candidate procedures, evaluation, and documented seeds/repeated trials. Preserve failed trials and disclose sample counts. Missing metrics remain unavailable, not zero.

## 7. Researcher controls and history

- Adding a hypothesis creates a candidate to evaluate; it is not automatically validated.
- Pinning is a display/search preference and must not change scientific scores.
- Excluding a hypothesis removes it from active search but preserves history.
- Restoring returns an excluded candidate to search under documented rules.
- Re-evaluation should preserve prior results needed for reproducibility.
- Reports distinguish system rankings from researcher preferences.
- Audit events record action, entity, timestamp, and useful non-sensitive details.

## 8. Team responsibilities

- **Sai Narayana — remote technical lead:** discovery algorithm, hypothesis lifecycle, experiment planner, scientific correctness, shared contracts, technical integration.
- **Anuj Sharma — remote:** Nodus SDK research/integration, remote execution, parallel evaluation, benchmark runner, runtime/cost evidence.
- **Ram sir — onsite:** Flask integration/application wiring, backend integration tests, deployment/demo setup.
- **Barkha Rathi — onsite:** React researcher workflow, hypothesis/evidence UI, ranked reports/benchmark views, test scenarios, evidence capture, presentation.

Give each issue one primary owner. Agree on shared contracts before parallel code changes. Handoffs should name changed files/interfaces, exact test commands/results, blockers, remaining work, and reproduction steps.

## 9. Repository and AI-agent contract

1. Read this file, focused docs, and the assigned issue before coding.
2. Inspect the actual repository/branch; do not assume a proposed layout already exists.
3. Identify relevant interfaces, tests, and dependencies before changing code.
4. Keep changes focused on issue scope; flag necessary cross-issue changes.
5. Use a focused branch and PR. Avoid direct commits to `main` except an agreed emergency.
6. Add/update tests and docs when contracts or behavior change.
7. Report exact commands and real outcomes; distinguish passed, failed, skipped, and not run.
8. Never fabricate logs, benchmark values, provider task IDs, costs, screenshots, or verification.
9. Never commit credentials, local env files, hidden ground truth, or unintended generated artifacts.
10. Do not silently change stack, scope, API contracts, metrics, or scientific semantics. Record durable decisions and seek review.
11. If docs, issues, and code disagree, surface it. Code/tests describe current behavior; approved docs describe intended direction; the issue defines task scope.
12. AI-generated code is not done until its owner can explain it, run it, and demonstrate normal, edge, and invalid cases.

## 10. Suggested repository layout

```text
CLARITY/
├── README.md
├── context.md
├── docs/
│   ├── architecture.md
│   ├── development.md
│   └── decisions.md
├── backend/
│   ├── app/
│   ├── tests/
│   └── requirements.txt
├── frontend/
│   ├── src/
│   └── package.json
└── .github/
    ├── workflows/
    └── pull_request_template.md
```

This is a guide, not a demand to create empty directories. Update it once the real skeleton is established.

## 11. Development sequence

1. Finish context, architecture, decision, and contribution documentation.
2. Establish application skeleton and automated checks.
3. Agree/test oracle, observation, hypothesis, evaluation, run, and benchmark contracts.
4. Build a deterministic vertical slice: generate hypothesis → evaluate → choose experiment → query oracle → update → ranked result.
5. Integrate real Nodus execution and parallelism behind an adapter.
6. Enforce budgets, lifecycle, persistence, and API behavior.
7. Build the React researcher workflow and hypothesis controls.
8. Add reports, metrics, and benchmark dashboard.
9. Validate edge cases, deploy cleanly, and rehearse the demo.

Integrate early and use small PRs. This sequence guides execution but does not replace issue scope.

## 12. Success criteria and guardrails

A credible demo shows a configured run, hidden oracle access without rule leakage, evidence-based candidate rankings, informative experiment selection, updated observations, accurate budget/status, persisted results, researcher controls that preserve history, measured active-versus-random results, and disclosed limitations. Genuine Nodus execution should be shown when access is available; otherwise disclose the blocker.

Never present mocked execution as real, fabricate scientific metrics, use model-generated confidence as a substitute for measured error, leak ground truth, or claim the top-ranked candidate is necessarily true.

## 13. Open questions

Track resolution in `docs/decisions.md`:
- First supported hypothesis family/grammar and oracle input/output domain.
- Candidate fitting objective, ranking formula, and complexity proxy.
- Disagreement/uncertainty score.
- Actual Nodus SDK version, task API, concurrency, native budget controls, cost metadata, retries, and cancellation.
- MySQL library/ORM and migration tool.
- Exact run states and cancellation semantics.
- Export formats and deployment target.
- Target error/accuracy threshold and trial count.

**Standard:** Build a real, testable scientific system, preserve evidence, and be honest about what measurements prove.
